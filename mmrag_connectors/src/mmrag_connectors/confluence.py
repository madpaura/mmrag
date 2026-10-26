"""Confluence Data Center / Server connector (REST API, bearer personal access token).

Layout: rendering a page as a document (pure functions) -> API access -> which pages to ingest -> linked Jira
tickets -> fetch.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterator
from urllib.parse import parse_qs, urlparse

import httpx

from .atlassian import AtlassianConnector, SlowResponse
from .base import ConnectorError, Document, Field_, register
from .htmlmd import html_to_md

EXPAND = "body.view,version,space,ancestors"
LIST_EXPAND = "version,ancestors"  # selecting pages: no bodies
JIRA_LINK = re.compile(r"https?://[^\s\"'<>]+/browse/([A-Z][A-Z0-9_]+-\d+)")


# ---- rendering -------------------------------------------------------------
def page_id_from(ref: str) -> str:
    """Accepts a page id, /pages/<id>/..., or ?pageId=<id> URL."""
    ref = ref.strip()
    if ref.isdigit():
        return ref
    m = re.search(r"/pages/(\d+)", ref)
    if m:
        return m.group(1)
    q = parse_qs(urlparse(ref).query).get("pageId")
    if q:
        return q[0]
    raise ConnectorError(f"cannot find a page id in '{ref}'")


def page_html(page: dict) -> str:
    return page.get("body", {}).get("view", {}).get("value", "")


def jira_refs(html: str) -> Iterator[tuple[str, str]]:
    """(jira host, issue key) for every /browse/KEY link in a page."""
    for m in JIRA_LINK.finditer(html):
        yield urlparse(m.group(0)).netloc, m.group(1)


def page_document(page: dict, base: str) -> Document:
    title = page["title"]
    space = page.get("space", {}).get("key", "")
    safe = re.sub(r"[^\w.\- ]+", "_", title).strip()[:120]
    return Document(
        external_id=f"confluence:{page['id']}", title=title,
        content=f"# {title}\n\n" + html_to_md(page_html(page)),
        file_source=f"confluence_{space}_{safe}__{page['id']}.md",
        url=base + page.get("_links", {}).get("webui", f"/pages/viewpage.action?pageId={page['id']}"),
        updated=page.get("version", {}).get("when", ""),
        meta={"space": space},
    )


# ---- connector ---------------------------------------------------------------
@register
class ConfluenceConnector(AtlassianConnector):
    type = "confluence"
    label = "Confluence (Data Center)"
    guide = "confluence"
    product = "Confluence"
    rate_limit_admin = "Administration > Rate limiting > Exemptions"
    batch = 25        # pages with bodies per request (rendering bodies is what makes Confluence slow)
    list_batch = 100  # pages per request while selecting (no bodies)
    walk_max = 20     # list children page by page while a level has at most this many pages, else one CQL search
    config_schema = AtlassianConnector.schema([
        Field_("roots", "Root pages", "textarea", help="Each root and its children are ingested."),
        Field_("space", "Space key", help="Ingest the space, from its top-level pages down to Depth. Ignored when "
                                         "root pages are set."),
        Field_("depth", "Depth", "number", default=3, help="Levels to ingest. 1 = the root pages only (for a space: "
                                                           "its top-level pages, usually the home page). 0 = all levels."),
        Field_("follow_jira", "Also ingest linked Jira tickets", "bool", default=False, help="Resolves /browse/KEY links found in pages."),
        Field_("jira_instances", "Jira instances", "textarea", help="base_url|token — one per line"),
    ], base_help="Include the context path, if any.")

    def __init__(self, config, log=None):
        super().__init__(config, log)
        self._cql_ok = True
        self._limits: dict[str, int] = {}  # page size per kind of request, once lowered after a slow answer
        self._slow = 0                     # pages skipped in a row because they alone took longer than the timeout

    def check(self) -> str:
        return f"{self.get_json('/rest/api/space', limit=1).get('size', 0)} space visible"

    # ---- API ----------------------------------------------------------------
    def _paged(self, path: str, kind: str, limit: int, **params) -> Iterator[dict]:
        """Every result of a start/limit listing. A response slower than the timeout is asked for again with half the
        page size, which then stays lower for this `kind` of request for the rest of the run."""
        limit = min(limit, self._limits.get(kind, limit))
        start = 0
        while True:
            try:
                d = self.get_json(path, limit=limit, start=start, **params)
            except SlowResponse:
                if limit == 1:
                    raise
                limit = self._limits[kind] = max(1, limit // 2)
                self.log(f"Confluence answered too slowly; asking for {limit} page(s) per request from now on")
                continue
            res = d.get("results", [])
            yield from res
            # Confluence links the next page; a server may cap the page size, so without links only a short page ends
            more = "next" in d["_links"] if "_links" in d else len(res) >= limit
            if not res or not more:
                return
            start += len(res)

    def _search(self, cql: str, expand: str, limit: int) -> Iterator[dict]:
        self.log(f"CQL: {cql}")
        yield from self._paged("/rest/api/content/search", "list", limit, cql=cql, expand=expand)

    def _no_cql(self, e: httpx.HTTPStatusError) -> None:
        self._cql_ok = False
        self.log(f"CQL search failed ({e.response.status_code}); walking child pages instead")

    @staticmethod
    def _since_clause(since: datetime | None) -> str:
        if not since:
            return ""
        # CQL dates are in the server's time zone; a day of margin is cheap (unchanged pages are skipped by hash)
        return f' and lastmodified >= "{(since - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")}"'

    # ---- selection (page stubs without bodies) -------------------------------------
    def _below(self, root: dict, depth: int, since: datetime | None) -> Iterator[dict]:
        """Pages under `root` whose level is below `depth` (root = level 0; depth 0 = all levels). Child listings are
        walked level by level, so a shallow selection never reads the rest of a big tree; when a level is wide (or
        depth is unlimited) one CQL search over the subtree takes fewer requests."""
        base = len(root.get("ancestors", []))
        level, n = [root["id"]], 1
        while level and (not depth or n < depth):
            if self._cql_ok and (not depth or len(level) > self.walk_max):
                try:
                    for p in self._search(f"ancestor = {root['id']} and type = page{self._since_clause(since)}",
                                          LIST_EXPAND, self.list_batch):
                        lv = len(p.get("ancestors", [])) - base
                        if lv >= n and (not depth or lv < depth):
                            yield p
                    return
                except httpx.HTTPStatusError as e:  # CQL search unavailable/blocked
                    self._no_cql(e)
            nxt = []
            for pid in level:
                for p in self._paged(f"/rest/api/content/{pid}/child/page", "list", self.list_batch, expand=LIST_EXPAND):
                    nxt.append(p["id"])
                    yield p
            level, n = nxt, n + 1

    def _space_top(self, space: str) -> Iterator[dict]:
        """The space's top-level pages (the home page, and any page created without a parent)."""
        try:
            yield from self._paged(f"/rest/api/space/{space}/content/page", "list", self.list_batch, depth="root",
                                   expand=LIST_EXPAND)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ConnectorError(f"space '{space}' not found (or not visible to this token)")
            raise

    def _stubs(self, depth: int, since: datetime | None) -> Iterator[dict]:
        """The configured selection, without bodies: root pages (or the space's top-level pages) down to `depth`."""
        roots = [page_id_from(r) for r in (self.config.get("roots") or "").splitlines() if r.strip()]
        space = (self.config.get("space") or "").strip()
        if not roots and not space:
            raise ConnectorError("configure at least one root page or a space key")
        if roots:
            if space:
                self.log(f"space '{space}' ignored: root pages are set, so only they (depth {depth or 'all'}) are ingested")
            for pid in roots:
                try:  # a missing root (deleted, moved) is skipped; access, rate limit and TLS errors fail the run
                    root = self.get_json(f"/rest/api/content/{pid}", expand=LIST_EXPAND)
                except httpx.HTTPStatusError as e:
                    self.log(f"skip page {pid}: {e}")
                    continue
                yield root
                yield from self._below(root, depth, since)
        elif not depth and self._cql_ok:
            try:
                yield from self._search(f'space = "{space}" and type = page{self._since_clause(since)}',
                                        LIST_EXPAND, self.list_batch)
                return
            except httpx.HTTPStatusError as e:
                self._no_cql(e)
            yield from self._stubs_from_top(space, depth, since)
        else:
            yield from self._stubs_from_top(space, depth, since)

    def _stubs_from_top(self, space: str, depth: int, since: datetime | None) -> Iterator[dict]:
        tops = list(self._space_top(space))
        self.log(f"space '{space}': {len(tops)} top-level page(s), depth {depth or 'all'}")
        for top in tops:
            yield top
            yield from self._below(top, depth, since)

    @staticmethod
    def _changed(stub: dict, since: datetime | None) -> bool:
        if not since:
            return True
        try:
            when = datetime.fromisoformat(stub["version"]["when"])
        except (KeyError, TypeError, ValueError):
            return True
        if when.tzinfo is None:
            when = when.replace(tzinfo=since.tzinfo)
        return when >= since - timedelta(days=1)

    # ---- bodies --------------------------------------------------------------------
    def _bodies(self, ids: list[str]) -> Iterator[dict]:
        """Full pages for `ids`, in that order: one CQL search per batch (smaller batches after a slow answer), or one
        request per page when CQL is unavailable. A page that alone takes longer than the timeout is skipped."""
        if self._cql_ok and len(ids) > 1:
            try:
                got = {}
                for p in self._paged("/rest/api/content/search", "bodies", len(ids), expand=EXPAND,
                                     cql=f"id in ({','.join(ids)})"):
                    got[p["id"]] = p
                    if len(got) == len(ids):
                        break
                yield from (got[i] for i in ids if i in got)
                return
            except SlowResponse:  # one of them is too slow even alone: find it, page by page
                self._limits["bodies"] = 2  # and don't let that one page slow down every later batch
            except httpx.HTTPStatusError as e:
                self._no_cql(e)
        for pid in ids:
            try:
                page = self.get_json(f"/rest/api/content/{pid}", expand=EXPAND)
            except httpx.HTTPStatusError as e:
                self.log(f"skip page {pid}: {e}")
                continue
            except SlowResponse as e:
                self._slow += 1
                if self._slow >= 3:  # not one heavy page: the server is struggling, so fail instead of skipping all
                    raise
                self.log(f"skip page {pid}: {e}")
                continue
            self._slow = 0
            yield page

    def _pages(self, since: datetime | None) -> Iterator[dict]:
        """The configured selection with bodies, each page once. Bodies are fetched in batches while pages are being
        selected, and only for pages changed since `since` (with a day of margin)."""
        depth = self.config.get("depth")
        depth = 3 if depth in (None, "") else max(0, int(depth))
        seen: set[str] = set()
        todo: list[str] = []
        for stub in self._stubs(depth, since):
            if stub["id"] in seen:
                continue
            seen.add(stub["id"])
            if self._changed(stub, since):
                todo.append(stub["id"])
            if len(todo) >= self._limits.get("bodies", self.batch):
                yield from self._bodies(todo)
                todo = []
        if todo:
            yield from self._bodies(todo)
        self.log(f"{len(seen)} page(s) selected" + (" (unchanged ones are not downloaded)" if since else ""))

    # ---- linked Jira tickets -------------------------------------------------------
    def _jira_instances(self) -> dict:
        """{host: JiraConnector} from 'base_url|token' lines; TLS and pacing settings follow this source."""
        from .jira import JiraConnector
        out = {}
        for line in (self.config.get("jira_instances") or "").splitlines():
            if "|" in line:
                url, tok = [s.strip() for s in line.split("|", 1)]
                out[urlparse(url).netloc] = JiraConnector(
                    {"base_url": url, "token": tok, "mode": "keys", "verify_ssl": self.config["verify_ssl"],
                     "max_rps": self.config.get("max_rps")}, self.log)
        return out

    def _linked_jira(self, refs: dict[str, set[str]]) -> Iterator[Document]:
        clients = self._jira_instances()
        try:
            for host, keys in refs.items():
                if host not in clients:
                    self.log(f"no Jira instance configured for {host}; skipping {len(keys)} ticket(s)")
                    continue
                yield from clients[host].fetch_keys(keys)
        finally:
            for c in clients.values():
                c.close()

    # ---- documents --------------------------------------------------------------
    def fetch(self, since: datetime | None = None) -> Iterator[Document]:
        follow = bool(self.config.get("follow_jira"))
        refs: dict[str, set[str]] = {}
        n = 0
        for page in self._pages(since):
            n += 1
            doc = page_document(page, self.base)
            self.log(f"page {page['id']}: {doc.title}")
            if follow:
                for host, key in jira_refs(page_html(page)):
                    refs.setdefault(host, set()).add(key)
            yield doc
        self.log(f"{n} page(s) fetched with {self.http.requests} request(s)"
                 + (f", {self.http.throttled} rate-limited" if self.http.throttled else ""))
        if refs:
            yield from self._linked_jira(refs)
