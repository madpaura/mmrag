# mmrag-connectors

Reusable Python package used by the management portal, which can also be used on its own:

* **Connectors** turn a data source into `Document`s (markdown text + a unique `file_source` name):
  `confluence` (Data Center/Server REST, PAT) and `jira` (Data Center/Server REST v2, PAT).
* **`LightRAGClient`**: a thin client for the LightRAG server API (insert, upload, track, delete, clear, query).

```bash
pip install ./mmrag_connectors                                        # from this repo
pip install "http://mydomain.com/mmrag/<proj>/mgmt/downloads/mmrag_connectors.zip"   # or from any project portal
```

Ready-to-run samples (also downloadable from the portal's **Guides → Developers**, pre-filled with the project URL):

| Sample | Run |
| --- | --- |
| `src/mmrag_connectors/samples/confluence_ingest.py` | `CONFLUENCE_TOKEN=… python confluence_ingest.py --base-url https://confluence.example.com --root 123456 --depth 3` |
| `src/mmrag_connectors/samples/jira_ingest.py` | `JIRA_TOKEN=… python jira_ingest.py --base-url https://jira.example.com --project ABC` |

Both skip unchanged content on re-runs (`--state` file) and replace changed content.

## Use it in code

```python
from mmrag_connectors import get, LightRAGClient

conf = get("confluence")({"base_url": "https://confluence.example.com", "token": "…",
                          "roots": "https://confluence.example.com/pages/viewpage.action?pageId=123", "depth": 3})
lr = LightRAGClient("http://mydomain.com/mmrag/p1/api")
tracks = [lr.upsert_text(d.content, d.file_source) for d in conf.fetch()]   # insert or replace
lr.wait(tracks)           # polls /documents/track_status until PROCESSED / FAILED
```

### Connector options
| Connector | Key options |
| --- | --- |
| `confluence` | `base_url`, `token`, `roots` (page URLs/ids, one per line), `space`, `depth`, `follow_jira` + `jira_instances` (`url|token` lines), `max_rps`, `verify_ssl` |
| `jira` | `base_url`, `token`, `mode` = `project`/`jql`/`keys`, `project`, `jql`, `keys`, `include_comments`, `link_depth`, `max_issues`, `max_rps`, `verify_ssl` |

`fetch(since=datetime)` is an incremental hint (Jira adds `updated >= since`). Callers should still de-duplicate by `Document.content_hash`, as the portal does.

## LightRAG behaviour you must know (verified against v1.5.2)
* LightRAG stores only the **basename** of `file_source` and refuses a basename it already has with **HTTP 409**. Connectors produce flat, unique names (`confluence_<SPACE>_<title>__<id>.md`, `jira_<host>_<KEY>.md`).
* To replace a document: `delete_documents([doc_id])`, wait for the background deletion to finish, then insert again. While insert gets 409, it isn't finished.
* While the pipeline is processing, `delete_documents()` and `clear()` are **refused** (HTTP 200 with `status: busy`). The client raises `LightRAGBusy`, and you retry when the pipeline is idle.
* Deleting needs the embedding service to be up (the vector index is re-flushed).

## Writing a new connector (plugin)

```python
from mmrag_connectors import Connector, Document, Field_, register

@register                     # or expose it via an entry point (below)
class EdmConnector(Connector):
    type = "edm"
    label = "EDM"
    guide = ""                # optional: guides/<name>.md shown in the portal
    config_schema = [Field_("base_url", "Base URL", required=True), Field_("token", "Token", "password", required=True)]

    def test(self) -> str:
        return "ok"

    def fetch(self, since=None):
        for item in my_api_listing(self.config):
            yield Document(external_id=f"edm:{item.id}", title=item.title, content=item.markdown,
                           file_source=f"edm_{item.id}.md", url=item.link, updated=item.modified)
```

As a separate package, register it in that package's `pyproject.toml`. The portal picks it up automatically, renders the config form from `config_schema`, and schedules it like the built-ins:

```toml
[project.entry-points."mmrag.connectors"]
edm = "my_pkg.edm:EdmConnector"
```

To ship it inside the portal image, `pip install` it in `portal/Dockerfile`.

### Package layout

| Module | Role |
| --- | --- |
| `base.py` | The contract: `Connector`, `Document`, `Field_`, `register`, plugin discovery |
| `atlassian.py` | `AtlassianConnector`: shared base for Data Center REST products (connection fields, bearer client, rate limiting, error handling) |
| `confluence.py`, `jira.py` | Each is laid out as: pure rendering functions, then API access, then selection (what to ingest), then `fetch` |
| `ratelimit.py`, `tls.py`, `htmlmd.py` | Request pacing and 429 handling, company CA trust, HTML to markdown |

A connector for another REST source with a bearer token can subclass `AtlassianConnector` the same way: it gets `get_json()`, `test()` and `close()`, and implements `check()` and `fetch()`. Connectors are context managers (`with get("jira")(cfg) as j: ...`) and release their connections on exit.

## Tests
`pytest mmrag_connectors/tests` runs against the simulators in `../simulators` (no network needed).
