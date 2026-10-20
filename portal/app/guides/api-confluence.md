# Sample: Confluence (wiki) → LightRAG

Fetches Confluence (Data Center) pages through the REST API, converts them to markdown and ingests them into this project's LightRAG at `{{API}}`.

## 1. Install and get a token

```bash
pip install "{{PKG}}"
curl -O "{{SAMPLE_URL}}"
export LIGHTRAG_API_KEY='<access key>'          # Settings → Access in this portal
export CONFLUENCE_TOKEN=<personal access token>   # Confluence → profile → Settings → Personal Access Tokens
```

## 2. Run it

```bash
# a page and two levels of children
python confluence_ingest.py --base-url https://confluence.example.com --root 123456 --depth 3

# several roots (ids or page URLs), or a whole space
python confluence_ingest.py --base-url https://confluence.example.com \
    --root https://confluence.example.com/spaces/ENG/pages/123456/Home --root 654321
python confluence_ingest.py --base-url https://confluence.example.com --space ENG

# preview without ingesting
python confluence_ingest.py --base-url https://confluence.example.com --root 123456 --dry-run
```

Typical output:

```
Confluence: ok (1 space visible)
  · page 123456: Home
queued confluence_ENG_Home__123456.md
…
5 queued, 0 unchanged
waiting for LightRAG (entity extraction, graph, embeddings)…
  processed  confluence_ENG_Home__123456.md
done
```

| Option | Meaning |
| --- | --- |
| `--root` | Page id or URL; repeatable. Each root and its children are ingested |
| `--space` | Space key: every page of the space |
| `--depth` | Levels below each root (`1` = root only) |
| `--state` | File that remembers content hashes, so re-runs only send changed pages |
| `--lightrag-url` | Defaults to this project: `{{API}}` |
| `--insecure` | Skip TLS verification (self-signed internal servers) |

## 3. How it works

1. `get("confluence")(config)` builds the connector; `.test()` checks the token.
2. `.fetch()` yields one `Document` per page: markdown `content` and a unique `file_source`.
3. `LightRAGClient.upsert_text()` inserts it, or replaces the previous version.
4. `LightRAGClient.wait()` follows processing until each page is processed or failed.

## The script
