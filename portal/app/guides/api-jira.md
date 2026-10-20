# Sample: Jira → LightRAG

Fetches Jira (Data Center) issues through the REST API, turns each into a markdown document (fields, description, linked issues, comments) and ingests them into this project's LightRAG at `{{API}}`.

## 1. Install and get a token

```bash
pip install "{{PKG}}"
curl -O "{{SAMPLE_URL}}"
export LIGHTRAG_API_KEY='<access key>'          # Settings → Access in this portal
export JIRA_TOKEN=<personal access token>   # Jira → profile → Personal Access Tokens
```

## 2. Run it

```bash
# a whole project
python jira_ingest.py --base-url https://jira.example.com --project ABC

# a project, but only open bugs, plus issues linked one hop away
python jira_ingest.py --base-url https://jira.example.com --project ABC \
    --filter "issuetype = Bug AND status != Closed" --link-depth 1

# any JQL, or explicit keys
python jira_ingest.py --base-url https://jira.example.com --jql "labels = rag AND updated >= -7d"
python jira_ingest.py --base-url https://jira.example.com --keys ABC-1,ABC-7

# preview without ingesting
python jira_ingest.py --base-url https://jira.example.com --project ABC --dry-run
```

| Option | Meaning |
| --- | --- |
| `--project` / `--jql` / `--keys` | What to ingest (pick one) |
| `--filter` | Extra JQL appended to `--project` |
| `--link-depth` | Follow issue links N hops |
| `--no-comments` | Leave comments out |
| `--max-issues` | Upper limit per run (default 1000) |
| `--state` | File that remembers content hashes, so re-runs only send changed issues |
| `--lightrag-url` | Defaults to this project: `{{API}}` |

Run it from cron for a nightly sync — e.g. `--jql "project = ABC AND updated >= -1d"`.

## 3. How it works

1. `get("jira")(config)` builds the connector; `.test()` checks the token.
2. `.fetch()` pages through `/rest/api/2/search` and yields one `Document` per issue.
3. `LightRAGClient.upsert_text()` inserts it, or replaces the previous version.
4. `LightRAGClient.wait()` follows processing until each issue is processed or failed.

## The script
