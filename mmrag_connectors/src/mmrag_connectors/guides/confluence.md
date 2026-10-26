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
| Space key | Optional. Ingest the space, starting from its top-level pages (usually just the home page) down to *Depth*. Ignored when root pages are set. |
| Depth | Levels to ingest. `1` = the root pages only (with a space key: its top-level pages), `2` = those and their children, and so on. `0` = all levels. |
| Request timeout | Seconds to wait for one Confluence answer (default 120). |
| Linked Jira tickets | Resolves `/browse/KEY` links. Jira instances: one `base_url|token` per line. |

## 3. Test, then run

Press **Test connection**, then **Run now**. With *daily* or *weekly* updates, only pages whose content changed are re-ingested.

## Rate limiting

Confluence Data Center limits REST requests per user. The connector keeps the request count low. It first selects pages without their content, reading only the levels *Depth* asks for (one search per subtree when a level is wide or *Depth* is `0`). Then it downloads the selected pages' content 25 at a time. Scheduled runs only download pages modified since the last run. It stays under *Max requests per second* (default 2), slows down when the server says its budget is nearly used, and waits out `429` answers for the server's `Retry-After` time.

If runs still fail with *kept rate-limiting*:
- lower **Max requests per second** (e.g. `1`), and use a daily schedule instead of frequent manual full runs;
- best: ask the Confluence admin to **exempt the integration account** (*Administration → Rate limiting → Exemptions*), ideally a dedicated service account whose token you use here.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `401` / `403` | The token expired or has no access to the space. |
| *did not answer within … s* | Rendering pages with heavy macros is slow. The connector already retries with fewer pages per request and skips a page that alone takes longer than the timeout. If many pages are skipped, or the run fails after three slow pages in a row, raise **Request timeout** (e.g. `300`) or lower *Depth*. |
| `429` / *kept rate-limiting* | See *Rate limiting* above. |
| Certificate errors | Internal CA: untick *Verify TLS*, or add your CA to the portal image. |
| Empty pages | Macros that render nothing server-side (e.g. some draw.io diagrams) produce no text. |
