# Confluence (Data Center)

Ingests the pages you choose, and their child pages down to *Depth*, as markdown. Tables are kept as markdown tables.

## 1. Create a personal access token

1. In Confluence, open your profile picture → **Settings** → **Personal Access Tokens**.
2. Select **Create token**, name it, set an expiry, and copy it. It is shown once.
3. The token inherits your permissions: it can only read what you can read.

## 2. Configure the source

| Field | What to enter |
| --- | --- |
| Base URL | `https://confluence.example.com`, including the context path if any (e.g. `/wiki`) |
| Root pages | One page URL or id per line. Each root and its children are ingested. |
| Space key | Optional. Ingest every page of the space instead of or in addition to roots. |
| Depth | Levels below each root page. `1` = root only. |
| Linked Jira tickets | Resolves `/browse/KEY` links. Jira instances: one `base_url|token` per line. |

## 3. Test, then run

Press **Test connection**, then **Run now**. With *daily* or *weekly* updates, only pages whose content changed are re-ingested.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `401` / `403` | The token expired or has no access to the space. |
| Certificate errors | Internal CA: untick *Verify TLS*, or add your CA to the portal image. |
| Empty pages | Macros that render nothing server-side (e.g. some draw.io diagrams) produce no text. |
