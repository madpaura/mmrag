"""Presentation helpers: turn DB rows into what the templates render (labels, colours, history, summaries)."""
from __future__ import annotations

from datetime import datetime
import re
from urllib.parse import urlparse

# run.status -> design status kind
KIND = {"completed": "ok", "completed_with_errors": "warn", "failed": "bad", "interrupted": "bad",
        "cancelled": "stop", "pending": "run", "fetching": "run", "monitoring": "run"}
KIND_TEXT = {"ok": "Completed", "warn": "Completed with errors", "bad": "Failed", "stop": "Cancelled", "run": "In progress"}
KIND_SHORT = {"ok": "Completed", "warn": "With errors", "bad": "Failed", "stop": "Cancelled", "run": "Running"}
KIND_COLOR = {"ok": "var(--ok)", "warn": "var(--warn)", "bad": "var(--bad)", "stop": "var(--neu)", "run": "var(--acc)"}
TYPE_LABEL = {"confluence": "Confluence", "jira": "Jira", "files": "Files"}
DOC_STATUS = {"processed": ("Processed", "ok"), "failed": ("Failed", "bad"), "submitted": ("Pending", "warn"),
              "processing": ("Processing", "acc")}


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def fmt_short(s):  # Oct 19, 15:00
    d = _dt(s)
    return f"{d:%b} {d.day}, {d:%H:%M}" if d else "—"


def fmt_sec(s):  # Oct 19, 15:00:30
    d = _dt(s)
    return f"{d:%b} {d.day}, {d:%H:%M:%S}" if d else "—"


def fmt_full(s):  # 2026-10-19 15:00:30
    d = _dt(s)
    return f"{d:%Y-%m-%d %H:%M:%S}" if d else "—"


def fmt_time(s):  # 15:00:30
    d = _dt(s)
    return f"{d:%H:%M:%S}" if d else ""


def run_view(r: dict) -> dict:
    k = KIND.get(r["status"], "bad")
    found = r["discovered"] or 0
    pct = lambda n: round(100 * n / found) if found else 0  # noqa: E731
    return {**r, "kind": k, "text": KIND_TEXT[k], "short": KIND_SHORT[k], "active": k == "run",
            "summary": f"{found} found · {r['unchanged']} unchanged · {r['processed']} processed · {r['failed']} failed",
            "pPr": pct(r["processed"]), "pUn": pct(r["unchanged"]), "pFl": pct(r["failed"]),
            "lbl": {"ok": "ok", "warn": "warn", "bad": "bad", "stop": "", "run": "acc"}[k]}


def hist(runs: list[dict]) -> list[dict]:
    """runs newest-first -> squares oldest-first."""
    out = []
    for r in reversed(runs):
        k = KIND.get(r["status"], "bad")
        out.append({"color": KIND_COLOR[k], "title": f"Run #{r['id']} · {KIND_TEXT[k]}", "kind": k})
    return out


def hist_label(h: list[dict]) -> str:
    if not h:
        return "No runs yet"
    if len(h) == 1:
        return "Last run " + h[0]["title"].split(" · ")[1].lower()
    c = {k: sum(1 for x in h if x["kind"] == k) for k in ("ok", "warn", "bad", "stop", "run")}
    parts = [f"{c['ok']} completed"] + [f"{c[k]} {w}" for k, w in (("stop", "cancelled"), ("warn", "with errors"),
                                                                   ("bad", "failed"), ("run", "running")) if c[k]]
    return f"Last {len(h)} runs: " + ", ".join(parts)


def source_desc(src: dict) -> str:
    c, t = src["config"], src["type"]
    if t == "files":
        return "Files uploaded from this portal."
    if t == "confluence":
        roots = [x.strip() for x in (c.get("roots") or "").splitlines() if x.strip()]
        bits = []
        if c.get("space"):
            bits.append(f"Confluence space {c['space']}")
        if roots:
            bits.append(f"{len(roots)} root page{'s' if len(roots) > 1 else ''}, depth {c.get('depth') or 1}")
        host = urlparse(c.get("base_url") or "").netloc
        return (", ".join(bits) or "Confluence") + (f" on {host}." if host else ".")
    if t == "jira":
        mode = c.get("mode") or "project"
        what = {"project": f"Jira project {c.get('project') or '?'}", "jql": "Jira issues matching a JQL query",
                "keys": "Selected Jira issues"}.get(mode, "Jira")
        host = urlparse(c.get("base_url") or "").netloc
        return what + (f" on {host}." if host else ".")
    return src["type"]


def health(runs: list[dict]) -> tuple[str, str]:
    """(label, lbl-class) from the latest finished run."""
    done = [r for r in runs if KIND.get(r["status"]) != "run"]
    if not done:
        return ("Never run", "")
    k = KIND.get(done[0]["status"], "bad")
    return {"ok": ("Healthy", "ok"), "warn": ("Errors in last run", "warn"), "bad": ("Last run failed", "bad"),
            "stop": ("Last run cancelled", "")}[k]


def doc_status(st: str) -> tuple[str, str]:
    return DOC_STATUS.get(st, (st.title(), ""))


def _norm(err: str) -> tuple[str, str]:
    """(group title, normalised detail) for an error message."""
    detail = re.sub(r"doc-[0-9a-f]{8,}(-chunk-\d+)?", "doc-…", err)
    detail = re.sub(r"0x[0-9a-f]+", "0x…", detail)
    m = re.search(r"raised (\w+)", detail)
    if detail.startswith("RetryError") and m:
        return f"LLM / embedding call failed ({m.group(1)})", detail
    head = detail.split(":")[0].strip() if ":" in detail[:120] else detail[:80]
    if head.startswith("Error while deleting document"):
        head = "Error while deleting the previous version"
    return head, detail


LOG_ERR = re.compile(r"^(?:LightRAG failed|FAILED to submit) (.+?): (.*)$", re.S)


def group_errors(log: list[dict]) -> list[dict]:
    """Group a run's per-document failures (from its log, which survives later runs) by normalised message."""
    groups: dict[str, dict] = {}
    for line in log:
        m = LOG_ERR.match(line["msg"])
        if not m:
            continue
        title, detail = _norm(m.group(2))
        g = groups.setdefault(title, {"title": title, "detail": detail, "files": []})
        if m.group(1) not in g["files"]:
            g["files"].append(m.group(1))
    return list(groups.values())


CONNECTION_FIELDS = ("base_url", "token", "verify_ssl")


def form_layout(schema) -> tuple[list, list]:
    """Split a connector schema into the design's 'Connection' and 'What to ingest' steps.

    Short fields (text/number/select) sit side by side in a two-column grid, except a select or a field that
    starts a group; a textarea right after a checkbox is indented under it (e.g. Jira instances under the toggle)."""
    conn = [f for f in schema if f.name in CONNECTION_FIELDS or f.type == "password"]
    rest = [f for f in schema if f not in conn]
    rows: list[dict] = []
    i = 0
    while i < len(rest):
        f = rest[i]
        nxt = rest[i + 1] if i + 1 < len(rest) else None
        short = lambda x: x is not None and x.type in ("text", "number")  # noqa: E731
        if short(f) and short(nxt):
            rows.append({"kind": "g2", "fields": [f, nxt]})
            i += 2
            continue
        indent = f.type == "textarea" and i > 0 and rest[i - 1].type == "bool"
        rows.append({"kind": "one", "field": f, "indent": indent})
        i += 1
    return conn, rows


def recovery(name: str, runs: list[dict]) -> dict | None:
    """'X is healthy again' when the latest run(s) completed after earlier runs failed or had errors."""
    kinds = [(r["id"], KIND.get(r["status"], "bad"), r) for r in runs]  # newest first
    ok_streak = []
    for rid, k, _ in kinds:
        if k != "ok":
            break
        ok_streak.append(rid)
    rest = kinds[len(ok_streak):]
    bad_before = [x for x in rest if x[1] in ("warn", "bad")]
    if not ok_streak or not bad_before:
        return None
    oks = sorted(ok_streak)
    runs_txt = (f"Run #{oks[0]}" if len(oks) == 1 else
                "Runs " + ", ".join(f"#{i}" for i in oks[:-1]) + f" and #{oks[-1]}")
    n_bad = len([x for x in rest if x[1] != "ok"])
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}
    last_err_run = bad_before[0][2]
    return {"key": f"{name}:{oks[-1]}", "title": f"{name} is healthy again",
            "text": f"{runs_txt} completed after {words.get(n_bad, n_bad)} earlier run{'s' if n_bad != 1 else ''} "
                    f"failed or ended with errors.", "err_run": last_err_run["id"], "err": last_err_run.get("error")}
