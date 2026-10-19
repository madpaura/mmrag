#!/usr/bin/env python3
"""Ingest Jira (Data Center) issues into a LightRAG project.

    pip install "{{PACKAGE_URL}}"
    export JIRA_TOKEN=...                # personal access token (Jira > Profile > Personal Access Tokens)
    python jira_ingest.py --base-url https://jira.example.com --project ABC
    python jira_ingest.py --base-url https://jira.example.com --jql 'project = ABC AND updated >= -7d'
    python jira_ingest.py --base-url https://jira.example.com --keys ABC-1,ABC-7

Each issue becomes one markdown document (fields, description, linked issues, comments).
Re-running is safe: unchanged issues are skipped; changed ones replace the old version.
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from mmrag_connectors import ConnectorError, LightRAGClient, LightRAGError, get

LIGHTRAG_URL = "{{LIGHTRAG_URL}}"  # this project's LightRAG API


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--base-url", required=True, help="Jira base URL")
    ap.add_argument("--token", default=os.getenv("JIRA_TOKEN"), help="PAT (default: $JIRA_TOKEN)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--project", help="project key")
    g.add_argument("--jql", help="any JQL query")
    g.add_argument("--keys", help="comma-separated issue keys")
    ap.add_argument("--filter", default="", help="extra JQL appended to --project")
    ap.add_argument("--link-depth", type=int, default=0, help="also ingest linked issues N hops away")
    ap.add_argument("--no-comments", action="store_true")
    ap.add_argument("--max-issues", type=int, default=1000)
    ap.add_argument("--lightrag-url", default=os.getenv("LIGHTRAG_URL", LIGHTRAG_URL))
    ap.add_argument("--api-key", default=os.getenv("LIGHTRAG_API_KEY"))
    ap.add_argument("--state", default=".jira_ingest_state.json", help="remembers content hashes between runs")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--insecure", action="store_true", help="don't verify TLS certificates")
    a = ap.parse_args()
    if not a.token:
        ap.error("set --token or $JIRA_TOKEN")

    mode = "project" if a.project else "jql" if a.jql else "keys"
    # 1. connect to Jira
    jira = get("jira")({"base_url": a.base_url, "token": a.token, "mode": mode, "project": a.project or "",
                        "jql": a.jql or a.filter, "keys": a.keys or "", "link_depth": a.link_depth,
                        "include_comments": not a.no_comments, "max_issues": a.max_issues,
                        "verify_ssl": not a.insecure},
                       log=lambda m: print("  ·", m))
    try:
        print("Jira:", jira.test())
    except ConnectorError as e:
        print("Cannot connect to Jira:", e)
        return 1

    # 2. connect to LightRAG
    lr = LightRAGClient(a.lightrag_url, a.api_key)
    try:
        lr.health()
    except LightRAGError as e:
        print("Cannot reach LightRAG:", e)
        return 1

    # 3. fetch issues and ingest new / changed ones
    state_file = Path(a.state)
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    tracks, skipped = [], 0
    try:
        for doc in jira.fetch():                   # each issue -> markdown Document
            digest = hashlib.sha256(doc.content.encode()).hexdigest()
            if state.get(doc.file_source) == digest:
                skipped += 1
                continue
            if a.dry_run:
                print(f"would ingest {doc.title}  ({len(doc.content)} chars)  {doc.url}")
                continue
            try:
                tracks.append(lr.upsert_text(doc.content, doc.file_source))   # insert or replace
                state[doc.file_source] = digest
                print("queued", doc.title)
            except LightRAGError as e:
                print("FAILED", doc.title, "-", e)
    except ConnectorError as e:
        print("Jira error:", e)
        return 1
    if not a.dry_run:
        state_file.write_text(json.dumps(state, indent=1))
    print(f"{len(tracks)} queued, {skipped} unchanged")

    # 4. wait for LightRAG to process them
    if tracks and not a.no_wait:
        print("waiting for LightRAG (entity extraction, graph, embeddings)…")
        failed = 0
        for st in lr.wait(tracks, interval=5).values():
            for d in st.get("documents", []):
                ok = str(d.get("status")).upper() == "PROCESSED"
                failed += not ok
                print(f"  {d.get('status'):<10} {d.get('file_path')}  {d.get('error_msg') or ''}")
        print("done" if not failed else f"{failed} document(s) failed")
        return 1 if failed else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
