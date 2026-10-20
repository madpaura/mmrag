# Python package & API

Ingest from your own scripts, CI jobs or other tools instead of the portal. Everything the portal does is available through this project's LightRAG API.

<div class="flow" aria-label="Pipeline"><span>Your script</span><span class="mut">→</span><span>Fetch (Confluence / Jira / anything)</span><span class="mut">→</span><span>LightRAG API</span><span class="mut">→</span><span>Processed / failed</span></div>

| | |
| --- | --- |
| LightRAG API | `{{API}}` |
| API reference | <a href="{{API}}/docs" target="_blank" rel="noopener">{{API}}/docs</a> |
| Python package | `pip install "{{PKG}}"` |
| Access key | Every API call needs `X-API-Key: <key>` — find it under <a href="{{base}}/settings#access">Settings → Access</a> |

## Authentication

The API only accepts requests that carry this project's access key. Keep it out of code — use an environment variable:

```bash
export LIGHTRAG_API_KEY='<access key from Settings → Access>'
```

The package and both samples read `LIGHTRAG_API_KEY` automatically. A request without a valid key gets **401**.

## 1. Install the package

`mmrag_connectors` contains the Confluence and Jira connectors used by this portal plus a small LightRAG client. Python 3.10+.

```bash
pip install "{{PKG}}"
```

## 2. Ready-made samples

| Sample | What it does |
| --- | --- |
| <a href="{{base}}/guides/api-confluence">Confluence (wiki)</a> | Root pages + children (or a whole space) → LightRAG |
| <a href="{{base}}/guides/api-jira">Jira</a> | A project, any JQL, or issue keys → LightRAG |

Both are single files you can run as-is or copy into your own code. Re-running them is safe: unchanged content is skipped and changed content replaces the old version.

## 3. The client in five lines

```python
import os
from mmrag_connectors import LightRAGClient

lr = LightRAGClient("{{API}}", api_key=os.environ["LIGHTRAG_API_KEY"])
track = lr.upsert_text("# Release notes\n\nVersion 2.1 adds …", "release-notes-2.1.md")  # insert or replace
print(lr.wait([track]))          # waits until LightRAG has processed it
print(lr.query("What does version 2.1 add?", mode="mix"))
```

| Method | Purpose |
| --- | --- |
| `upsert_text(text, name)` | Insert, or replace the document with that name (handles 409 / busy / async delete) |
| `insert_text(text, name)` | Insert only; raises `LightRAGError(status=409)` if the name exists |
| `upload_file(path)` | Upload PDF / DOCX / … for LightRAG to parse |
| `wait(track_ids)` | Poll until processed or failed |
| `find_by_file_path(name)` / `delete_documents(ids)` | Look up / remove documents (`LightRAGBusy` while processing) |
| `query(question, mode)` | Ask: `mix`, `hybrid`, `local`, `global`, `naive` |

## 4. Plain REST (any language)

```bash
# insert a text document
curl -X POST {{API}}/documents/text -H "X-API-Key: $LIGHTRAG_API_KEY" -H "Content-Type: application/json" \
  -d '{"text": "# Title\n\nBody…", "file_source": "my-doc.md"}'          # -> {"track_id": "…"}

# upload a file
curl -X POST {{API}}/documents/upload -H "X-API-Key: $LIGHTRAG_API_KEY" -F "file=@handbook.pdf"

# processing status
curl -H "X-API-Key: $LIGHTRAG_API_KEY" {{API}}/documents/track_status/<track_id>

# ask
curl -X POST {{API}}/query -H "X-API-Key: $LIGHTRAG_API_KEY" -H "Content-Type: application/json" -d '{"query": "…", "mode": "mix"}'
```

## Rules LightRAG enforces

| Rule | What to do |
| --- | --- |
| Only the **file name** (basename) of `file_source` is stored, and it must be unique | Give every document a unique name, e.g. `confluence_<SPACE>_<title>__<id>.md` |
| A name that already exists is rejected with **409** | Delete the old document first — or use `upsert_text` |
| Deletes are refused while the pipeline is **busy** | Retry when idle — `upsert_text` waits for you |
| Processing is asynchronous | Use the returned `track_id` to follow it |

<div class="note"><strong><svg class="i" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.25"></circle><path d="M8 7.25v4M8 4.9v.01"></path></svg>Note</strong>Documents ingested by scripts appear in LightRAG and in <em>Ask</em>, but not in this portal's Documents list, which shows what the portal's own sources ingested.</div>
