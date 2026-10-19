# mmrag: LightRAG deployer for internal projects

Each project gets its own isolated LightRAG stack and an ingestion portal, behind the host nginx:

```
http://mydomain.com/mmrag/<proj>/api/    LightRAG API (+ /docs, /webui)        → 127.0.0.1:<LIGHTRAG_PORT>
http://mydomain.com/mmrag/<proj>/mgmt/   management portal (ingestion, guides)  → 127.0.0.1:<PORTAL_PORT>
```

```
setup.sh                  deploy / down / status / list / backup / restore / destroy / check
stack/                    templates: docker-compose.yml, lightrag.env.tmpl (your tuned .env), nginx snippet
patches/pipeline.py       LightRAG patch (VLM skip) → mounted into the container. Written for **v1.5.2**
portal/                   FastAPI + HTMX management portal (Dockerfile builds from repo root)
mmrag_connectors/         reusable package: Confluence/Jira DC connectors + LightRAG client (+ README, sample)
simulators/               mock Confluence/Jira, mock LightRAG, mock LLM for development
projects/<proj>/          generated per project: .env, docker-compose.yml, nginx.conf, prompts/
backups/                  setup.sh backup output
data-src/                 original scripts (superseded by mmrag_connectors; kept for reference)
```

## Per-project stack (`mmrag-<proj>` compose project, own network + volumes)
`postgres` (pgvector: KV, vectors, doc status) · `neo4j` (graph) · `rag-app` (LightRAG v1.5.2 + patch) · `portal`.
Only `rag-app` and `portal` publish ports, and only on 127.0.0.1. LLM/embeddings go to the shared LiteLLM via `host.docker.internal:4000`.

## First-time host setup
1. `cp config/global.env.example config/global.env` and set `BASE_URL`.
2. nginx: add `include /etc/nginx/mmrag.d/*.conf;` to the server block for your domain (see `stack/nginx-server.example.conf`).
3. Review `stack/lightrag.env.tmpl` (created from `stack/lightrag.env.tmpl.example` on first `./setup.sh check`; git-ignored): models, LiteLLM API key, MinerU, tuning. Each new project starts from this file. DB passwords are generated per project.
4. `./setup.sh check`

## Operations
```bash
./setup.sh deploy p1          # first run creates projects/p1/.env (ports, passwords, URLs); later runs update + restart
./setup.sh list               # all projects with their URLs
./setup.sh status p1
./setup.sh backup p1          # stops p1 briefly (cold, consistent) → backups/p1-<ts>.tar.gz
./setup.sh restore p1 backups/p1-<ts>.tar.gz     # verifies archive + checksums + prerequisites first
./setup.sh restore p9 backups/p1-<ts>.tar.gz     # clone into a new project (URLs/ports rewritten)
./setup.sh down p1            # stop (data kept)
./setup.sh destroy p1         # stop and delete all data (asks for confirmation)
```
Project-specific LightRAG settings: edit `projects/<proj>/.env`, then `./setup.sh deploy <proj>`.
A backup contains every volume (postgres, neo4j, rag_storage, inputs, portal DB with sources/credentials), plus `.env`, compose, nginx snippet, prompts and the patch. **Treat backups as secrets.**

## Management portal (`/mmrag/<proj>/mgmt/`)
* **Sources**: Confluence (root pages + depth, or a whole space, optionally the Jira tickets linked from pages) and Jira (project / JQL / keys, comments, linked-issue depth). Each has *Test connection*, *Run now*, and daily/weekly schedules.
* **Pipeline**: fetch → skip unchanged (content hash) → send new docs → monitor LightRAG → replace changed docs (old version deleted once LightRAG is idle, then re-inserted). Scheduled runs are incremental.
* **Runs**: live progress (found / unchanged / sent / processed / failed), log, per-document errors, cancel.
* **Upload documents**: PDF/DOCX/MD/TXT…, parsed by LightRAG's parser. Re-uploading the same name replaces the old version.
* **Data**: delete a document, all data of a source, or everything (type the project name to confirm).
* **Guides** for each connector, and **Ask** to query the knowledge base.
* No authentication (as requested). Restrict access at nginx if needed.

## Development
```bash
python3 -m venv .venv && .venv/bin/pip install -e "mmrag_connectors[test]" -r portal/requirements.txt
.venv/bin/pytest portal/tests mmrag_connectors/tests      # 20 tests, all against simulators
```
See `simulators/README.md` to run the portal or a real LightRAG stack without Confluence, Jira or an LLM.

## Known limits / notes
* `patches/pipeline.py` only works with **LightRAG v1.5.2** (`:latest`, i.e. 1.5.7, fails at import). Upgrading LightRAG means re-basing the patch, then setting `LIGHTRAG_IMAGE`.
* LightRAG's Swagger page hard-codes `/openapi.json`. The nginx snippet rewrites it, so `/api/docs` works under the prefix.
* Pages deleted at the source are not removed automatically. Delete them in the portal.
* Confluence draw.io diagrams (supported by the old `data-src` scraper) aren't converted yet. Most render to no text.
* Connector tokens are stored in the portal DB (`portal_data` volume) unencrypted.
