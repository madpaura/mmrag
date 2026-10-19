"""Mock Confluence DC + Jira DC REST (subset) serving generated documents.

Run:  python mock_atlassian.py [--port 8090]
  Confluence base URL: http://localhost:8090/confluence   Jira base URL: http://localhost:8090/jira
  Token (both): sim-token
  Root page id: 1000, space key: SIM, Jira project: SIM
Mutate to test incremental sync:  POST /sim/touch/page/1001   POST /sim/touch/issue/SIM-3
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta, timezone

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request

TOKEN = "sim-token"
BASE = "http://localhost:8090"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def build():
    pages, issues = {}, {}
    pid = 1000
    pages[pid] = dict(id=str(pid), title="Simulated Home", parent=None, n=0)
    kids = []
    for i in range(1, 5):
        pid += 1
        pages[pid] = dict(id=str(pid), title=f"Subsystem {i} Overview", parent=1000, n=i)
        kids.append(pid)
    for p in kids:
        for j in range(1, 4):
            pid += 1
            pages[pid] = dict(id=str(pid), title=f"Subsystem {pages[p]['n']} Runbook {j}", parent=p, n=pid)
    for p in pages.values():
        p["rev"] = 1
        p["when"] = NOW
    for n in range(1, 13):
        issues[f"SIM-{n}"] = dict(key=f"SIM-{n}", summary=f"Simulated issue {n}: controller timeout in zone {n % 4}",
                                  status=["Open", "In Progress", "Done"][n % 3], rev=1, when=NOW + timedelta(hours=n),
                                  links=[f"SIM-{n + 1}"] if n < 12 and n % 3 == 0 else [])
    return pages, issues


PAGES, ISSUES = build()
app = FastAPI(title="mock-atlassian")


def auth(authorization: str = Header(default="")):
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "bad token")


def page_html(p, base: str | None = None) -> str:
    base = base or BASE
    refs = "".join(f'<li><a href="{base}/jira/browse/SIM-{(p["n"] % 12) + 1}">SIM-{(p["n"] % 12) + 1}</a></li>' for _ in range(1))
    return (f"<h1>{p['title']}</h1><p>This simulated page (revision {p['rev']}) describes the behaviour of component "
            f"{p['title']}. The retry limit is {3 + p['n'] % 5} and the watchdog interval is {10 * (1 + p['n'] % 7)} seconds.</p>"
            f"<h2>Parameters</h2><table><tr><th>Name</th><th>Value</th></tr><tr><td>retry</td><td>{3 + p['n'] % 5}</td></tr></table>"
            f"<h2>Related tickets</h2><ul>{refs}</ul>")


def page_json(p, expand="", base=None):
    out = {"id": p["id"], "type": "page", "title": p["title"], "space": {"key": "SIM"},
           "version": {"number": p["rev"], "when": iso(p["when"])},
           "_links": {"webui": f"/spaces/SIM/pages/{p['id']}/{p['title'].replace(' ', '+')}"}}
    if "body.view" in expand:
        out["body"] = {"view": {"value": page_html(p, base)}}
    return out


@app.get("/confluence/rest/api/space", dependencies=[Depends(auth)])
def spaces(limit: int = 25):
    return {"results": [{"key": "SIM"}], "size": 1}


@app.get("/confluence/rest/api/content/{pid}", dependencies=[Depends(auth)])
def get_page(pid: int, request: Request, expand: str = ""):
    if pid not in PAGES:
        raise HTTPException(404)
    return page_json(PAGES[pid], expand, f"{request.url.scheme}://{request.headers.get('host', 'localhost:8090')}")


@app.get("/confluence/rest/api/content/{pid}/child/page", dependencies=[Depends(auth)])
def children(pid: int, limit: int = 25, start: int = 0):
    res = [page_json(p) for p in PAGES.values() if p["parent"] == pid]
    return {"results": res[start:start + limit], "size": len(res)}


@app.get("/confluence/rest/api/content", dependencies=[Depends(auth)])
def space_pages(spaceKey: str, type: str = "page", limit: int = 25, start: int = 0):
    res = [page_json(p) for p in PAGES.values()] if spaceKey == "SIM" else []
    return {"results": res[start:start + limit], "size": len(res)}


def issue_json(i, fields):
    f = {"summary": i["summary"], "status": {"name": i["status"]}, "issuetype": {"name": "Bug"},
         "priority": {"name": "Medium"}, "assignee": {"displayName": "Sim User"}, "reporter": {"displayName": "Sim Reporter"},
         "created": iso(NOW), "updated": iso(i["when"]),
         "description": f"Controller times out after {30 + i['rev']} s under load. Revision {i['rev']}.",
         "comment": {"comments": [{"author": {"displayName": "Sim User"}, "created": iso(NOW),
                                   "body": "Reproduced on build 42; see linked ticket."}]},
         "issuelinks": [{"type": {"outward": "relates to"}, "outwardIssue": {"key": k, "fields": {"summary": ISSUES[k]["summary"]}}}
                        for k in i["links"]], "subtasks": []}
    return {"key": i["key"], "fields": f}


@app.get("/jira/rest/api/2/myself", dependencies=[Depends(auth)])
def myself():
    return {"displayName": "Sim User"}


@app.get("/jira/rest/api/2/search", dependencies=[Depends(auth)])
def search(jql: str, fields: str = "", startAt: int = 0, maxResults: int = 50):
    res = list(ISSUES.values())
    if m := re.search(r'project\s*=\s*"?(\w+)"?', jql):
        res = res if m.group(1) == "SIM" else []
    if m := re.search(r"key in \(([^)]*)\)", jql):
        keys = re.findall(r'"?([A-Z]+-\d+)"?', m.group(1))
        res = [i for i in res if i["key"] in keys]
    if m := re.search(r'\bkey\s*=\s*"?([A-Z]+-\d+)"?', jql):
        res = [i for i in res if i["key"] == m.group(1)]
    if m := re.search(r'updated >= "([\d\- :]+)"', jql):
        t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        res = [i for i in res if i["when"] >= t]
    return {"total": len(res), "startAt": startAt, "issues": [issue_json(i, fields) for i in res[startAt:startAt + maxResults]]}


@app.post("/sim/touch/page/{pid}")
def touch_page(pid: int):
    PAGES[pid]["rev"] += 1
    PAGES[pid]["when"] = datetime.now(timezone.utc)
    return {"ok": True, "rev": PAGES[pid]["rev"]}


@app.post("/sim/touch/issue/{key}")
def touch_issue(key: str):
    ISSUES[key]["rev"] += 1
    ISSUES[key]["when"] = datetime.now(timezone.utc)
    return {"ok": True, "rev": ISSUES[key]["rev"]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    BASE = f"http://localhost:{a.port}"
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
