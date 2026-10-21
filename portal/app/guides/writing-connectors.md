# Writing a connector

Connectors live in the `mmrag_connectors` package. A connector turns a data source into documents (markdown text plus a unique file name). The portal does everything else: forms, scheduling, de-duplication, LightRAG calls and monitoring.

## 1. Implement it

```python
from mmrag_connectors import Connector, Document, Field_, register

@register
class EdmConnector(Connector):
    type = "edm"
    label = "EDM"
    config_schema = [
        Field_("base_url", "Base URL", required=True),
        Field_("token", "Token", "password", required=True),
        Field_("folder", "Folder", help="Folder to ingest"),
    ]

    def test(self) -> str:
        return "ok"            # raise ConnectorError on bad credentials

    def fetch(self, since=None):
        for item in list_items(self.config):
            yield Document(external_id=f"edm:{item.id}", title=item.title,
                           content=item.markdown, file_source=f"edm_{item.id}.md",
                           url=item.link, updated=item.modified)
```

## 2. Rules that matter

| Rule | Why |
| --- | --- |
| `file_source` must be unique as a **basename** | LightRAG stores only the basename and rejects duplicates with HTTP 409 |
| `external_id` must be stable | it is how a changed document replaces its previous version |
| Honour `since` when the source can filter by date | scheduled runs pass the last successful run time |
| Name fields `base_url`, `token`, `verify_ssl` | they appear in the *Connection* step of the form; the rest in *What to ingest* |
| Release connections in `close()` | the portal closes each connector when a run or connection test ends |

### Sources with a REST API and a bearer token

Subclass `AtlassianConnector` (the base for the built-in Confluence and Jira connectors) instead of `Connector`. It adds the connection fields, request pacing with 429 handling, and clear errors for bad tokens or unreachable servers:

```python
from mmrag_connectors.atlassian import AtlassianConnector

@register
class EdmConnector(AtlassianConnector):
    type, label, product = "edm", "EDM", "EDM"
    config_schema = AtlassianConnector.schema([Field_("folder", "Folder")])

    def check(self) -> str:                      # shown as "ok (...)" by Test connection
        return self.get_json("/api/whoami")["name"]

    def fetch(self, since=None):
        for item in self.get_json("/api/items", folder=self.config["folder"])["items"]:
            yield Document(...)
```

## 3. Register it

Ship it as its own package and add an entry point:

```toml
[project.entry-points."mmrag.connectors"]
edm = "my_pkg.edm:EdmConnector"
```

Install it in the portal image (`pip install` in `portal/Dockerfile`) and redeploy with `./setup.sh deploy <project>`. It appears on **Sources → Add a connector**, with a form generated from `config_schema`.

To show a guide here, add `guides/<type>.md` to your package and set `guide = "<type>"` on the class.
