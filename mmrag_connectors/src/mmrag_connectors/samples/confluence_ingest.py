#!/usr/bin/env python3
"""Ingest Confluence (Data Center) pages into a LightRAG project.

    pip install "{{PACKAGE_URL}}"
    export LIGHTRAG_API_KEY=...          # this project's access key (portal > Settings > Access)
    export CONFLUENCE_TOKEN=...          # personal access token (Confluence > Settings > Personal Access Tokens)
    python confluence_ingest.py --base-url https://confluence.example.com --root 123456 --depth 2

Options: --root (repeatable, page id or URL), --space KEY (whole space), --depth N (levels below each root),
--dry-run (only list what would be ingested), --no-wait (don't wait for LightRAG to finish processing).
Re-running is safe: documents whose content is unchanged are skipped; changed ones replace the old version.
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
    ap.add_argument("--base-url", required=True, help="Confluence base URL, incl. context path")
    ap.add_argument("--token", default=os.getenv("CONFLUENCE_TOKEN"), help="PAT (default: $CONFLUENCE_TOKEN)")
    ap.add_argument("--root", action="append", default=[], help="root page id or URL (repeatable)")
    ap.add_argument("--space", help="space key: ingest every page of the space (ignored with --root)")
    ap.add_argument("--depth", type=int, default=2, help="levels below each root (1 = root only)")
    ap.add_argument("--lightrag-url", default=os.getenv("LIGHTRAG_URL", LIGHTRAG_URL))
    ap.add_argument("--api-key", default=os.getenv("LIGHTRAG_API_KEY"), help="project access key (default: $LIGHTRAG_API_KEY)")
    ap.add_argument("--state", default=".confluence_ingest_state.json", help="remembers content hashes between runs")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--insecure", action="store_true", help="don't verify TLS certificates")
    ap.add_argument("--max-rps", type=float, default=2, help="max requests/second to the server (rate limits)")
    a = ap.parse_args()
    if not a.token:
        ap.error("set --token or $CONFLUENCE_TOKEN")

    # 1. connect to Confluence
    wiki = get("confluence")({"base_url": a.base_url, "token": a.token, "roots": "\n".join(a.root),
                              "space": a.space or "", "depth": a.depth, "verify_ssl": not a.insecure, "max_rps": a.max_rps},
                             log=lambda m: print("  ·", m))
    try:
        print("Confluence:", wiki.test())
    except ConnectorError as e:
        print("Cannot connect to Confluence:", e)
        return 1

    # 2. connect to LightRAG
    lr = LightRAGClient(a.lightrag_url, a.api_key)
    try:
        lr.status_counts()                         # needs a valid key (unlike /health)
    except LightRAGError as e:
        if e.status in (401, 403):
            print("LightRAG rejected the access key: set $LIGHTRAG_API_KEY (portal > Settings > Access) or --api-key")
        else:
            print("Cannot reach LightRAG:", e)
        return 1

    # 3. fetch pages and ingest new / changed ones
    state_file = Path(a.state)
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    tracks, skipped = [], 0
    for doc in wiki.fetch():                       # each page -> markdown Document
        digest = hashlib.sha256(doc.content.encode()).hexdigest()
        if state.get(doc.file_source) == digest:
            skipped += 1
            continue
        if a.dry_run:
            print(f"would ingest {doc.file_source}  ({len(doc.content)} chars)  {doc.url}")
            continue
        try:
            tracks.append(lr.upsert_text(doc.content, doc.file_source))   # insert or replace
            state[doc.file_source] = digest
            print("queued", doc.file_source)
        except LightRAGError as e:
            print("FAILED", doc.file_source, "-", e)
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
