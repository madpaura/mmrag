# Simulators (development without real systems)

| Script | Default port | Simulates |
| --- | --- | --- |
| `mock_atlassian.py` | 8090 | Confluence DC (`/confluence`) + Jira DC (`/jira`). Token `sim-token`, 17 pages (root id `1000`, space `SIM`), 12 issues (project `SIM`) with links |
| `mock_lightrag.py` | 9621 | LightRAG API subset: insert/upload/track/delete/clear/paginated/query, **including** real quirks: basename 409s, `busy` refusal while processing, async deletion. `FAIL_ME` in text → FAILED |
| `mock_llm.py` | 4001 | OpenAI-compatible LLM + embeddings (stand-in for LiteLLM), so a *real* LightRAG stack can ingest, delete and query with no model |

```bash
python simulators/mock_atlassian.py &           # or: docker compose -f simulators/docker-compose.sim.yml up -d
python simulators/mock_llm.py &
# change content at the "source" to test incremental sync:
curl -X POST localhost:8090/sim/touch/page/1001
curl -X POST localhost:8090/sim/touch/issue/SIM-3
```

**Run the portal locally against mocks** (no Docker):
```bash
python simulators/mock_lightrag.py --port 9622 &
LIGHTRAG_URL=http://localhost:9622 PORTAL_DATA_DIR=./.portal-dev uvicorn app.main:app --app-dir portal --port 8080
# open http://localhost:8080/ and add a Confluence source: base URL http://localhost:8090/confluence, token sim-token, root 1000
```

**Real LightRAG stack with no LLM**: in `projects/<proj>/.env`, set `LLM_BINDING_HOST` and `EMBEDDING_BINDING_HOST` to `http://host.docker.internal:4001/v1`, `VLM_PROCESS_ENABLE=false`, `LIGHTRAG_PARSER=*:legacy-R`. Then `docker compose up -d rag-app` in that project dir. Containers reach host mocks via `host.docker.internal`.
