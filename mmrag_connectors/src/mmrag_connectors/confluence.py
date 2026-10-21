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

from .atlassian import AtlassianConnector
from .base import ConnectorError, Document, Field_, register
from .htmlmd import html_to_md

EXPAND = "body.view,version,space,ancestors"
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
    batch = 25  # pages (with bodies) per request
    config_schema = AtlassianConnector.schema([
        Field_("roots", "Root pages", "textarea", help="Each root and its children are ingested."),
        Field_("space", "Space key", help="Ingest every page of the space. Ignored when root pages are set."),
        Field_("depth", "Depth", "number", default=3, help="Levels below each root. 1 = root only."),
        Field_("follow_jira", "Also ingest linked Jira tickets", "bool", default=False, help="Resolves /browse/KEY links found in pages."),
        Field_("jira_instances", "Jira instances", "textarea", help="base_url|token — one per line"),
    ], base_help="Include the context path, if any.")

    def check(self) -> str:
        return f"{self.get_json('/rest/api/space', limit=1).get('size', 0)} space visible"

    # ---- API ----------------------------------------------------------------
    def _page(self, pid: str) -> dict:
        return self.get_json(f"/rest/api/content/{pid}", expand=EXPAND)

    def _paged(self, path: str, **params) -> Iterator[dict]:
        start = 0
        while True:
            d = self.get_json(path, limit=self.batch, start=start, **params)
            res = d.get("results", [])
            yield from res
            if len(res) < self.batch:
                return
            start += len(res)

    def _cql(self, cql: str) -> Iterator[dict]:
        self.log(f"CQL: {cql}")
        yield from self._paged("/rest/api/content/search", cql=cql, expand=EXPAND)

    @staticmethod
    def _since_clause(since: datetime | None) -> str:
        if not since:
            return ""
        # CQL dates are in the server's time zone; a day of margin is cheap (unchanged pages are skipped by hash)
        return f' and lastmodified >= "{(since - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")}"'

    # ---- selection ------------------------------------------------------------
    def _descendants(self, root: dict, depth: int, since: datetime | None) -> Iterator[dict]:
        """All pages below `root` down to `depth` levels, bodies included, in as few requests as possible."""
        if depth <= 1:
            return
        base_level = len(root.get("ancestors", []))
        try:
            for p in self._cql(f"ancestor = {root['id']} and type = page{self._since_clause(since)}"):
                if len(p.get("ancestors", [])) - base_level <= depth - 1:
                    yield p
            return
        except httpx.HTTPStatusError as e:  # CQL search unavailable/blocked -> walk children (bodies inline)
            self.log(f"CQL search failed ({e.response.status_code}); walking child pages instead")
        level = [root["id"]]
        for _ in range(depth - 1):
            nxt = []
            for pid in level:
                for p in self._paged(f"/rest/api/content/{pid}/child/page", expand=EXPAND):
                    nxt.append(p["id"])
                    yield p
            level = nxt

    def _tree(self, pid: str, depth: int, since: datetime | None) -> Iterator[dict]:
        """A root page and its descendants. A missing root (deleted, moved) is skipped; ConnectorError
        (access, rate limit, TLS) propagates and fails the run."""
        try:
            root = self._page(pid)
        except httpx.HTTPStatusError as e:
            self.log(f"skip page {pid}: {e}")
            return
        yield root
        yield from self._descendants(root, depth, since)

    def _pages(self, since: datetime | None) -> Iterator[dict]:
        """The configured selection, each page once: root pages down to `depth`, or else the whole space."""
        depth = max(1, int(self.config["depth"] or 1))
        roots = [page_id_from(r) for r in (self.config.get("roots") or "").splitlines() if r.strip()]
        space = (self.config.get("space") or "").strip()
        if not roots and not space:
            raise ConnectorError("configure at least one root page or a space key")
        if roots:
            if space:
                self.log(f"space '{space}' ignored: root pages are set, so only they (depth {depth}) are ingested")
            pages = (p for pid in roots for p in self._tree(pid, depth, since))
        else:
            pages = self._cql(f'space = "{space}" and type = page{self._since_clause(since)}')
        seen: set[str] = set()
        for p in pages:
            if p["id"] not in seen:
                seen.add(p["id"])
                yield p

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
