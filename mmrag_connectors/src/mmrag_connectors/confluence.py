"""Confluence Data Center / Server connector (REST API, bearer personal access token)."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterator
from urllib.parse import unquote, urlparse, parse_qs

import httpx

from . import tls
from .base import Connector, ConnectorError, Document, Field_, register
from .htmlmd import html_to_md

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
        Field_("space", "Space key", help="Ingest every page of the space, instead of or in addition to roots."),
        Field_("depth", "Depth", "number", default=3, help="Levels below each root. 1 = root only."),
        Field_("follow_jira", "Also ingest linked Jira tickets", "bool", default=False, help="Resolves /browse/KEY links found in pages."),
        Field_("jira_instances", "Jira instances", "textarea", help="base_url|token — one per line"),
        Field_("verify_ssl", "Verify TLS certificates", "bool", default=True, help="Turn off only for self-signed internal servers."),
    ]

    def __init__(self, config, log=None):
        super().__init__(config, log)
        c = self.config
        self.base = c["base_url"].rstrip("/")
        self.http = httpx.Client(headers={"Authorization": f"Bearer {c['token']}"}, timeout=60,
                                 verify=tls.verify_for(bool(c["verify_ssl"])), follow_redirects=True)

    def _get(self, path: str, **params):
        try:
            r = self.http.get(self.base + path, params=params)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:  # TLS / DNS / refused: fail the run, don't skip
            raise ConnectorError(f"cannot reach Confluence: {tls.explain(e, self.base)}")
        if r.status_code in (401, 403):
            raise ConnectorError(f"Confluence denied access ({r.status_code}) for {path}")
        r.raise_for_status()
        return r.json()

    def test(self) -> str:
        try:
            d = self._get("/rest/api/space", limit=1)
        except httpx.HTTPError as e:
            raise ConnectorError(f"cannot reach Confluence: {tls.explain(e, self.base)}")
        return f"ok ({d.get('size', 0)} space visible)"

    def _page(self, pid: str) -> dict:
        return self._get(f"/rest/api/content/{pid}", expand="body.view,version,space")

    def _children(self, pid: str) -> list[str]:
        ids, start = [], 0
        while True:
            d = self._get(f"/rest/api/content/{pid}/child/page", limit=100, start=start)
            ids += [p["id"] for p in d.get("results", [])]
            if len(d.get("results", [])) < 100:
                return ids
            start += 100

    def _space_pages(self, key: str) -> list[str]:
        ids, start = [], 0
        while True:
            d = self._get("/rest/api/content", spaceKey=key, type="page", limit=100, start=start)
            ids += [p["id"] for p in d.get("results", [])]
            if len(d.get("results", [])) < 100:
                return ids
            start += 100

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
        seen: set[str] = set()
        queue: list[tuple[str, int]] = []
        for ref in (self.config.get("roots") or "").splitlines():
            if ref.strip():
                queue.append((page_id_from(ref), depth))
        if self.config.get("space"):
            queue += [(pid, 1) for pid in self._space_pages(self.config["space"].strip())]
        if not queue:
            raise ConnectorError("configure at least one root page or a space key")
        jira_keys: dict[str, set[str]] = {}
        while queue:
            pid, d = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            try:
                page = self._page(pid)
            except httpx.HTTPError as e:  # e.g. 404: page deleted or moved -> skip it
                self.log(f"skip page {pid}: {e}")
                continue
            # ConnectorError (401/403: bad or expired token) propagates and fails the run
            doc = self._doc(page)
            self.log(f"page {pid}: {doc.title}")
            if self.config.get("follow_jira"):
                for m in re.finditer(JIRA_LINK, page.get("body", {}).get("view", {}).get("value", "")):
                    host = urlparse(m.group(0)).netloc
                    jira_keys.setdefault(host, set()).add(m.group(1))
            doc.meta.pop("html", None)
            yield doc
            if d > 1:
                queue += [(c, d - 1) for c in self._children(pid)]
        if self.config.get("follow_jira") and jira_keys:
            clients = self._jira_clients()
            for host, keys in jira_keys.items():
                if host not in clients:
                    self.log(f"no Jira instance configured for {host}; skipping {len(keys)} ticket(s)")
                    continue
                clients[host].config["keys"] = ",".join(sorted(keys))
                yield from clients[host].fetch()
