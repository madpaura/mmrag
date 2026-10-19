"""Sample: pull from Confluence/Jira with mmrag_connectors and push into a LightRAG project — no portal needed.

    pip install ./mmrag_connectors
    export LIGHTRAG_URL=http://mydomain.com/mmrag/p1/api      # or http://localhost:9621
    export CONFLUENCE_URL=https://confluence.example.com CONFLUENCE_TOKEN=... CONFLUENCE_ROOT=123456
    python sample_ingest.py confluence
    export JIRA_URL=https://jira.example.com JIRA_TOKEN=... JIRA_PROJECT=ABC
    python sample_ingest.py jira

Try it against the simulators (see simulators/README.md):
    CONFLUENCE_URL=http://localhost:8090/confluence CONFLUENCE_TOKEN=sim-token CONFLUENCE_ROOT=1000 \
    LIGHTRAG_URL=http://localhost:9622 python sample_ingest.py confluence
"""
import os
import sys

from mmrag_connectors import LightRAGClient, LightRAGError, get

CONFIGS = {
    "confluence": lambda: {"base_url": os.environ["CONFLUENCE_URL"], "token": os.environ["CONFLUENCE_TOKEN"],
                           "roots": os.environ.get("CONFLUENCE_ROOT", ""), "space": os.environ.get("CONFLUENCE_SPACE", ""),
                           "depth": int(os.environ.get("DEPTH", "2"))},
    "jira": lambda: {"base_url": os.environ["JIRA_URL"], "token": os.environ["JIRA_TOKEN"],
                     "project": os.environ["JIRA_PROJECT"], "max_issues": int(os.environ.get("MAX_ISSUES", "50"))},
}


def main(kind: str) -> None:
    connector = get(kind)(CONFIGS[kind](), log=print)
    print("connection:", connector.test())
    lr = LightRAGClient(os.environ.get("LIGHTRAG_URL", "http://localhost:9621"), os.environ.get("LIGHTRAG_API_KEY"))
    tracks = []
    for doc in connector.fetch():
        try:
            tracks.append(lr.insert_text(doc.content, doc.file_source))
            print("queued", doc.file_source)
        except LightRAGError as e:
            # 409 = LightRAG already has a document with this (base)name; delete it first to replace it
            print("skipped", doc.file_source, "-", e)
    print(f"waiting for LightRAG to process {len(tracks)} document(s)…")
    for tid, st in lr.wait(tracks, interval=5).items():
        for d in st.get("documents", []):
            print(f"  {d['status']:<10} {d['file_path']} {d.get('error_msg') or ''}")
    print(lr.status_counts())


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "confluence")
