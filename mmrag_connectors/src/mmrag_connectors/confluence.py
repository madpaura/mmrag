"""Confluence Data Center / Server connector (REST API, bearer personal access token)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterator
from urllib.parse import unquote, urlparse, parse_qs

import httpx

from . import tls
from .ratelimit import RateLimitedClient
from .base import Connector, ConnectorError, Document, Field_, register
from .htmlmd import html_to_md

EXPAND = "body.view,version,space,ancestors"
JIRA_LINK = re.compile(r"https?://[^\s\"'<>]+/browse/([A-Z][A-Z0-9_]+-\d+)")


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


@register
class ConfluenceConnector(Connector):
    type = "confluence"
    label = "Confluence (Data Center)"
    guide = "confluence"
    config_schema = [
        Field_("base_url", "Base URL", required=True, help="Include the context path, if any."),
        Field_("token", "Personal access token", "password", required=True),
        Field_("roots", "Root pages", "textarea", help="Each root and its children are ingested."),
        Field_("space", "Space key", help="Ingest every page of the space. Ignored when root pages are set."),
        Field_("depth", "Depth", "number", default=3, help="Levels below each root. 1 = root only."),
        Field_("follow_jira", "Also ingest linked Jira tickets", "bool", default=False, help="Resolves /browse/KEY links found in pages."),
        Field_("jira_instances", "Jira instances", "textarea", help="base_url|token — one per line"),
        Field_("max_rps", "Max requests per second", "number", default=2,
               help="Stays under Confluence's rate limit; 429 answers are waited out (Retry-After)."),
        Field_("verify_ssl", "Verify TLS certificates", "bool", default=True, help="Turn off only for self-signed internal servers."),
    ]

    def __init__(self, config, log=None):
        super().__init__(config, log)
        c = self.config
        self.base = c["base_url"].rstrip("/")
        self.http = RateLimitedClient(headers={"Authorization": f"Bearer {c['token']}"},
                                      verify=tls.verify_for(bool(c["verify_ssl"])),
                                      max_rps=float(c.get("max_rps") or 2), log=self.log)
        self.batch = 25  # pages (with bodies) per request

    def _get(self, path: str, **params):
        try:
            r = self.http.get(self.base + path, params=params)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:  # TLS / DNS / refused: fail the run, don't skip
            raise ConnectorError(f"cannot reach Confluence: {tls.explain(e, self.base)}")
        if r.status_code in (401, 403):
            raise ConnectorError(f"Confluence denied access ({r.status_code}) for {path}")
        if r.status_code in (429, 503):
            raise ConnectorError(f"Confluence kept rate-limiting ({r.status_code}) after {self.http.max_retries} retries. "
                                 "Lower 'Max requests per second', or ask the Confluence admin to exempt this account "
                                 "(Administration > Rate limiting > Exemptions).")
        r.raise_for_status()
        return r.json()

    def test(self) -> str:
        try:
            d = self._get("/rest/api/space", limit=1)
        except httpx.HTTPError as e:
            raise ConnectorError(f"cannot reach Confluence: {tls.explain(e, self.base)}")
        return f"ok ({d.get('size', 0)} space visible)"

    def _page(self, pid: str) -> dict:
        return self._get(f"/rest/api/content/{pid}", expand=EXPAND)

    def _paged(self, path: str, **params) -> Iterator[dict]:
        start = 0
        while True:
            d = self._get(path, limit=self.batch, start=start, **params)
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

    def _doc(self, p: dict) -> Document:
        html = p.get("body", {}).get("view", {}).get("value", "")
        title = p["title"]
        space = p.get("space", {}).get("key", "")
        body = f"# {title}\n\n" + html_to_md(html)
        safe = re.sub(r"[^\w.\- ]+", "_", title).strip()[:120]
        link = self.base + p.get("_links", {}).get("webui", f"/pages/viewpage.action?pageId={p['id']}")
        return Document(
            external_id=f"confluence:{p['id']}", title=title, content=body,
            file_source=f"confluence_{space}_{safe}__{p['id']}.md", url=link,
            updated=p.get("version", {}).get("when", ""),
            meta={"space": space, "html": html},
        )

    def _jira_clients(self):
        from .jira import JiraConnector
        out = {}
        for line in (self.config.get("jira_instances") or "").splitlines():
            if "|" in line:
                url, tok = [s.strip() for s in line.split("|", 1)]
                out[urlparse(url).netloc] = JiraConnector({"base_url": url, "token": tok, "mode": "keys"}, self.log)
        return out

    def fetch(self, since: datetime | None = None) -> Iterator[Document]:
        depth = max(1, int(self.config["depth"] or 1))
        roots = [page_id_from(r) for r in (self.config.get("roots") or "").splitlines() if r.strip()]
        space = (self.config.get("space") or "").strip()
        if not roots and not space:
            raise ConnectorError("configure at least one root page or a space key")
        seen: set[str] = set()
        jira_keys: dict[str, set[str]] = {}

        def emit(page: dict) -> Iterator[Document]:
            if page["id"] in seen:
                return
            seen.add(page["id"])
            doc = self._doc(page)
            self.log(f"page {page['id']}: {doc.title}")
            if self.config.get("follow_jira"):
                for m in re.finditer(JIRA_LINK, page.get("body", {}).get("view", {}).get("value", "")):
                    jira_keys.setdefault(urlparse(m.group(0)).netloc, set()).add(m.group(1))
            doc.meta.pop("html", None)
            yield doc

        for pid in roots:
            try:
                root = self._page(pid)
            except httpx.HTTPStatusError as e:  # e.g. 404: page deleted or moved -> skip it
                self.log(f"skip page {pid}: {e}")
                continue
            # ConnectorError (401/403, rate limit, TLS) propagates and fails the run
            yield from emit(root)
            for p in self._descendants(root, depth, since):
                yield from emit(p)
        if space and roots:
            self.log(f"space '{space}' ignored: root pages are set, so only they (depth {depth}) are ingested")
        elif space:
            for p in self._cql(f'space = "{space}" and type = page{self._since_clause(since)}'):
                yield from emit(p)
        self.log(f"{len(seen)} page(s) fetched with {self.http.requests} request(s)"
                 + (f", {self.http.throttled} rate-limited" if self.http.throttled else ""))
        if self.config.get("follow_jira") and jira_keys:
            clients = self._jira_clients()
            for host, keys in jira_keys.items():
                if host not in clients:
                    self.log(f"no Jira instance configured for {host}; skipping {len(keys)} ticket(s)")
                    continue
                clients[host].config["keys"] = ",".join(sorted(keys))
                yield from clients[host].fetch()
