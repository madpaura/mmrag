# mmrag — deploy & manage

Each project = its own LightRAG + Postgres + Neo4j + portal, behind the host nginx at
`<BASE_URL>/mmrag/<proj>/api/` (LightRAG) and `<BASE_URL>/mmrag/<proj>/mgmt/` (portal).

## 1. One-time host setup

Prerequisites: Docker + Compose v2, nginx, curl, tar, openssl, and LiteLLM reachable at `:4000`.

```bash
git clone https://github.com/madpaura/mmrag.git && cd mmrag

cp config/global.env.example config/global.env
vi config/global.env              # BASE_URL=https://mydomain.com

./setup.sh check                  # creates stack/lightrag.env.tmpl on first run
vi stack/lightrag.env.tmpl        # set LLM_BINDING_API_KEY / EMBEDDING_BINDING_API_KEY, models

# nginx: add to the server {} block of your domain, then reload
#   include /etc/nginx/mmrag.d/*.conf;
sudo nginx -t && sudo nginx -s reload
./setup.sh check                  # all ✓ (LiteLLM, nginx include, disk)
```

Use HTTPS on that server block. The access key is sent with every request.

## 2. Create a project

```bash
./setup.sh deploy p1              # ports, DB passwords, access key, nginx route, health wait
./setup.sh key p1                 # show the access key
```

| | URL | Login |
| --- | --- | --- |
| Portal | `<BASE_URL>/mmrag/p1/mgmt/` | user `p1` / password = key |
| LightRAG web UI | `<BASE_URL>/mmrag/p1/api/webui/` | same |
| API (scripts) | `<BASE_URL>/mmrag/p1/api/` | header `X-API-Key: <key>` |

Give the key only to that project's team. Other projects reject it.

## 3. Day-to-day

```bash
./setup.sh list                   # all projects + URLs
./setup.sh status p1              # containers + health
./setup.sh deploy p1              # apply changes (edit projects/p1/.env first) / upgrade portal
./setup.sh down p1                # stop (data kept); start again with: ./setup.sh deploy p1
./setup.sh rotate-key p1          # new key everywhere; share it again
```

Per-project LightRAG tuning (models, chunk size, …): edit `projects/p1/.env`, then run `./setup.sh deploy p1`.

## 4. Backup & restore

```bash
./setup.sh backup p1                                  # brief stop -> backups/p1-<timestamp>.tar.gz
./setup.sh restore p1 backups/p1-20261020-0100.tar.gz # checks archive first, asks before overwriting
./setup.sh restore p9 backups/p1-20261020-0100.tar.gz # clone into a new project (URLs/ports rewritten)
```

Backups contain everything (data, `.env` with keys and passwords), so store them like secrets.
Nightly via cron:

```cron
0 2 * * *  cd /opt/mmrag && ./setup.sh backup p1 >> backups/cron.log 2>&1
```

## 5. Remove a project

```bash
./setup.sh backup p1              # optional, last copy
./setup.sh destroy p1             # type the name to confirm; deletes data + nginx route
```

## 6. Ingest data

* **Portal:** Sources → New source (Confluence / Jira), or Upload documents.
* **Scripts:** Portal → Guides → Developers:
  ```bash
  pip install "<BASE_URL>/mmrag/p1/mgmt/downloads/mmrag_connectors.zip"
  export LIGHTRAG_API_KEY=<key> JIRA_TOKEN=<jira PAT>
  python jira_ingest.py --base-url https://jira.example.com --project ABC
  ```

## 7. Troubleshooting

| Symptom | Check |
| --- | --- |
| Browser spins, no login prompt | You can't reach the server or port (VPN/LAN/firewall). Test: `curl -I <BASE_URL>/mmrag/p1/mgmt/` |
| 401 everywhere | Wrong key / project name; `./setup.sh key p1` |
| 404 on the URL | nginx doesn't include `/etc/nginx/mmrag.d/*.conf`, or it wasn't reloaded |
| Documents fail in a run | Open the run page: errors are grouped. Usually LiteLLM or embeddings are down (`curl localhost:4000/health/liveliness`) |
| `rag-app` unhealthy | `docker logs mmrag-p1-rag-app-1`; the LightRAG image must stay `v1.5.2` (matches `patches/pipeline.py`) |
| Change won't delete / "busy" | LightRAG refuses deletes while processing; retry when the pipeline is idle |
| Logs | `docker compose --project-directory projects/p1 logs -f rag-app portal` |
