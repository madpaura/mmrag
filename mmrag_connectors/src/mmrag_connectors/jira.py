"""Jira Data Center / Server connector (REST API v2, bearer personal access token).

Layout: rendering an issue as markdown (pure functions) -> API access -> which issues to ingest -> fetch.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Iterator
from urllib.parse import urlparse

from .atlassian import AtlassianConnector
from .base import ConnectorError, Document, Field_, register
from .htmlmd import html_to_md

FIELDS = "summary,description,status,assignee,reporter,priority,issuetype,created,updated,comment,issuelinks,subtasks"
PAGE = 100       # issues per search request
KEY_CHUNK = 50   # keys per "key in (...)" query


# ---- rendering -------------------------------------------------------------
def _name(v) -> str:
    if isinstance(v, dict):
        return v.get("displayName") or v.get("name") or v.get("value") or "N/A"
    return str(v) if v else "N/A"


def _text(s: str | None) -> str:
    if not s:
        return ""
    return html_to_md(s) if "<" in s and ">" in s else s.strip()


def _linked(link: dict) -> dict:
    return link.get("outwardIssue") or link.get("inwardIssue") or {}


def linked_keys(issue: dict) -> list[str]:
    return [k for l in issue.get("fields", {}).get("issuelinks", []) if (k := _linked(l).get("key"))]


def issue_markdown(issue: dict, include_comments: bool = True) -> str:
    f = issue.get("fields", {})
    out = [f"# {issue['key']}: {f.get('summary', '')}", "", "| Field | Value |", "| --- | --- |"]
    for label, k in (("Type", "issuetype"), ("Status", "status"), ("Priority", "priority"),
                     ("Assignee", "assignee"), ("Reporter", "reporter")):
        out.append(f"| {label} | {_name(f.get(k))} |")
    out += [f"| Created | {f.get('created', 'N/A')} |", f"| Updated | {f.get('updated', 'N/A')} |", "",
            "## Description", "", _text(f.get("description")) or "_No description._"]
    links = f.get("issuelinks", [])
    if links:
        out += ["", "## Linked issues"]
        for l in links:
            o = _linked(l)
            rel = l.get("type", {}).get("outward" if "outwardIssue" in l else "inward") or "relates to"
            out.append(f"- {rel} {o.get('key', '?')}: {o.get('fields', {}).get('summary', '')}")
    comments = f.get("comment", {}).get("comments", [])
    if include_comments and comments:
        out += ["", "## Comments"]
        for cm in comments:
            out.append(f"**{_name(cm.get('author'))}** [{cm.get('created', '')}]: {_text(cm.get('body'))}\n")
    return "\n".join(out) + "\n"


def keys_jql(keys: Iterable[str]) -> str:
    return "key in (%s)" % ",".join(f'"{k}"' for k in keys)


# ---- connector ---------------------------------------------------------------
@register
class JiraConnector(AtlassianConnector):
    type = "jira"
    label = "Jira (Data Center)"
    guide = "jira"
    product = "Jira"
    rate_limit_admin = "Administration > System > Rate limiting > Exemptions"
    config_schema = AtlassianConnector.schema([
        Field_("mode", "Mode", "select", default="project", options=["project", "jql", "keys"]),
        Field_("project", "Project key", help="Used in project mode."),
        Field_("keys", "Issue keys", help="Used in keys mode, comma separated."),
        Field_("jql", "JQL", "textarea", help="In jql mode, the query. In project mode, an extra filter."),
        Field_("link_depth", "Linked-issue depth", "number", default=0, help="0 = only matching issues; N = follow links N hops."),
        Field_("max_issues", "Max issues", "number", default=5000, help="Upper limit per run."),
        Field_("include_comments", "Include comments", "bool", default=True),
    ], base_help="e.g. https://jira.example.com")

    def __init__(self, config, log=None):
        super().__init__(config, log)
        u = urlparse(self.base)
        self.host = re.sub(r"[^\w.-]", "_", u.netloc + u.path).strip("_")

    def check(self) -> str:
        d = self.get_json("/rest/api/2/myself")
        return f"authenticated as {d.get('displayName') or d.get('name')}"

    # ---- API ----------------------------------------------------------------
    def _search(self, jql: str, limit: int) -> Iterator[dict]:
        start = 0
        while start < limit:
            d = self.get_json("/rest/api/2/search", jql=jql, fields=FIELDS, startAt=start,
                              maxResults=min(PAGE, limit - start))
            issues = d.get("issues", [])
            yield from issues
            start += len(issues)
            if not issues or start >= d.get("total", 0):
                return

    def _by_keys(self, keys: list[str]) -> Iterator[dict]:
        for i in range(0, len(keys), KEY_CHUNK):
            yield from self._search(keys_jql(keys[i:i + KEY_CHUNK]), KEY_CHUNK)

    # ---- selection ------------------------------------------------------------
    def _jql(self, since: datetime | None) -> str:
        c = self.config
        mode = c["mode"]
        if mode == "keys":
            keys = [k.strip() for k in (c.get("keys") or "").split(",") if k.strip()]
            if not keys:
                raise ConnectorError("no issue keys given")
            return keys_jql(keys)
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

    def _issues(self, since: datetime | None) -> Iterator[dict]:
        """Issues matching the configuration, then issues linked to them up to `link_depth` hops; each once."""
        seen: set[str] = set()
        frontier: list[str] = []
        for issue in self._search(self._jql(since), int(self.config["max_issues"] or 5000)):
            seen.add(issue["key"])
            self.log(f"issue {issue['key']}")
            frontier += linked_keys(issue)
            yield issue
        for _ in range(int(self.config.get("link_depth") or 0)):
            nxt = [k for k in dict.fromkeys(frontier) if k not in seen]
            if not nxt:
                return
            frontier = []
            for issue in self._by_keys(nxt):
                if issue["key"] in seen:
                    continue
                seen.add(issue["key"])
                frontier += linked_keys(issue)
                yield issue

    # ---- documents --------------------------------------------------------------
    def _doc(self, issue: dict) -> Document:
        key, f = issue["key"], issue.get("fields", {})
        return Document(external_id=f"jira:{self.base}:{key}", title=f"{key}: {f.get('summary', '')}",
                        content=issue_markdown(issue, self.config["include_comments"]),
                        file_source=f"jira_{self.host}_{key}.md", url=f"{self.base}/browse/{key}",
                        updated=f.get("updated", ""), meta={"links": linked_keys(issue)})

    def fetch(self, since: datetime | None = None) -> Iterator[Document]:
        for issue in self._issues(since):
            yield self._doc(issue)

    def fetch_keys(self, keys: Iterable[str]) -> Iterator[Document]:
        """Exactly these issues (no link following), e.g. tickets referenced from Confluence pages."""
        for issue in self._by_keys(sorted(set(keys))):
            self.log(f"issue {issue['key']}")
            yield self._doc(issue)
