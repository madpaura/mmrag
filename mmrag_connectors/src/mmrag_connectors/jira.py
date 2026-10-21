"""Jira Data Center / Server connector (REST API v2, bearer personal access token)."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterator

import httpx
from urllib.parse import urlparse

from . import tls
from .ratelimit import RateLimitedClient
from .base import Connector, ConnectorError, Document, Field_, register
from .htmlmd import html_to_md

FIELDS = "summary,description,status,assignee,reporter,priority,issuetype,created,updated,comment,issuelinks,subtasks"


def _name(v) -> str:
    if isinstance(v, dict):
        return v.get("displayName") or v.get("name") or v.get("value") or "N/A"
    return str(v) if v else "N/A"


def _text(s: str | None) -> str:
    if not s:
        return ""
    return html_to_md(s) if "<" in s and ">" in s else s.strip()


@register
class JiraConnector(Connector):
    type = "jira"
    label = "Jira (Data Center)"
    guide = "jira"
    config_schema = [
        Field_("base_url", "Base URL", required=True, help="e.g. https://jira.example.com"),
        Field_("token", "Personal access token", "password", required=True),
        Field_("mode", "Mode", "select", default="project", options=["project", "jql", "keys"]),
        Field_("project", "Project key", help="Used in project mode."),
        Field_("keys", "Issue keys", help="Used in keys mode, comma separated."),
        Field_("jql", "JQL", "textarea", help="In jql mode, the query. In project mode, an extra filter."),
        Field_("link_depth", "Linked-issue depth", "number", default=0, help="0 = only matching issues; N = follow links N hops."),
        Field_("max_issues", "Max issues", "number", default=5000, help="Upper limit per run."),
        Field_("max_rps", "Max requests per second", "number", default=2,
               help="Stays under Jira's rate limit; 429 answers are waited out (Retry-After)."),
        Field_("include_comments", "Include comments", "bool", default=True),
        Field_("verify_ssl", "Verify TLS certificates", "bool", default=True, help="Turn off only for self-signed internal servers."),
    ]

    def __init__(self, config, log=None):
        super().__init__(config, log)
        c = self.config
        self.base = c["base_url"].rstrip("/")
        self.host = re.sub(r"[^\w.-]", "_", urlparse(self.base).netloc + urlparse(self.base).path).strip("_")
        self.http = RateLimitedClient(headers={"Authorization": f"Bearer {c['token']}"},
                                      verify=tls.verify_for(bool(c["verify_ssl"])),
                                      max_rps=float(c.get("max_rps") or 2), log=self.log)

    def _get(self, path: str, **params):
        try:
            r = self.http.get(self.base + path, params=params)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:  # TLS / DNS / refused: fail the run, don't skip
            raise ConnectorError(f"cannot reach Jira: {tls.explain(e, self.base)}")
        if r.status_code in (401, 403):
            raise ConnectorError(f"Jira denied access ({r.status_code}) for {path}")
        if r.status_code in (429, 503):
            raise ConnectorError(f"Jira kept rate-limiting ({r.status_code}) after {self.http.max_retries} retries. "
                                 "Lower 'Max requests per second', or ask the Jira admin to exempt this account.")
        r.raise_for_status()
        return r.json()

    def test(self) -> str:
        try:
            d = self._get("/rest/api/2/myself")
        except httpx.HTTPError as e:
            raise ConnectorError(f"cannot reach Jira: {tls.explain(e, self.base)}")
        return f"ok (authenticated as {d.get('displayName') or d.get('name')})"

    def _jql(self, since: datetime | None) -> str:
        c = self.config
        mode = c["mode"]
        if mode == "keys":
            keys = [k.strip() for k in (c.get("keys") or "").split(",") if k.strip()]
            if not keys:
                raise ConnectorError("no issue keys given")
            return "key in (%s)" % ",".join(f'"{k}"' for k in keys)
        parts = []
        if mode == "project":
            if not c.get("project"):
                raise ConnectorError("project key is required for mode=project")
            parts.append(f'project = "{c["project"].strip()}"')
        if c.get("jql"):
            parts.append(f"({c['jql'].strip()})")
        if not parts:
            raise ConnectorError("JQL is required for mode=jql")
        if since:
            parts.append(f'updated >= "{since.strftime("%Y-%m-%d %H:%M")}"')
        return " AND ".join(parts) + " ORDER BY updated DESC"

    def _search(self, jql: str, limit: int) -> Iterator[dict]:
        start = 0
        while start < limit:
            d = self._get("/rest/api/2/search", jql=jql, fields=FIELDS, startAt=start, maxResults=min(100, limit - start))
            issues = d.get("issues", [])
            yield from issues
            start += len(issues)
            if not issues or start >= d.get("total", 0):
                return

    def _doc(self, issue: dict) -> Document:
        f = issue.get("fields", {})
        key = issue["key"]
        out = [f"# {key}: {f.get('summary', '')}", "", "| Field | Value |", "| --- | --- |"]
        for label, k in (("Type", "issuetype"), ("Status", "status"), ("Priority", "priority"),
                         ("Assignee", "assignee"), ("Reporter", "reporter")):
            out.append(f"| {label} | {_name(f.get(k))} |")
        out += [f"| Created | {f.get('created', 'N/A')} |", f"| Updated | {f.get('updated', 'N/A')} |", "",
                "## Description", "", _text(f.get("description")) or "_No description._"]
        links = [l for l in f.get("issuelinks", [])]
        if links:
            out += ["", "## Linked issues"]
            for l in links:
                o = l.get("outwardIssue") or l.get("inwardIssue") or {}
                rel = (l.get("type", {}).get("outward") if "outwardIssue" in l else l.get("type", {}).get("inward")) or "relates to"
                out.append(f"- {rel} {o.get('key', '?')}: {o.get('fields', {}).get('summary', '')}")
        comments = f.get("comment", {}).get("comments", [])
        if self.config["include_comments"] and comments:
            out += ["", "## Comments"]
            for cm in comments:
                out.append(f"**{_name(cm.get('author'))}** [{cm.get('created', '')}]: {_text(cm.get('body'))}\n")
        return Document(external_id=f"jira:{self.base}:{key}", title=f"{key}: {f.get('summary', '')}",
                        content="\n".join(out) + "\n", file_source=f"jira_{self.host}_{key}.md",
                        url=f"{self.base}/browse/{key}", updated=f.get("updated", ""),
                        meta={"links": [(l.get("outwardIssue") or l.get("inwardIssue") or {}).get("key") for l in links]})

    def fetch(self, since: datetime | None = None) -> Iterator[Document]:
        limit = int(self.config["max_issues"] or 5000)
        seen: set[str] = set()
        frontier: list[str] = []
        for issue in self._search(self._jql(since), limit):
            seen.add(issue["key"])
            doc = self._doc(issue)
            self.log(f"issue {issue['key']}")
            frontier += [k for k in doc.meta["links"] if k]
            yield doc
        for _ in range(int(self.config.get("link_depth") or 0)):
            nxt = [k for k in dict.fromkeys(frontier) if k not in seen]
            frontier = []
            if not nxt:
                break
            for i in range(0, len(nxt), 50):
                chunk = nxt[i:i + 50]
                jql = "key in (%s)" % ",".join(f'"{k}"' for k in chunk)
                for issue in self._search(jql, 50):
                    if issue["key"] in seen:
                        continue
                    seen.add(issue["key"])
                    doc = self._doc(issue)
                    frontier += [k for k in doc.meta["links"] if k]
                    yield doc
