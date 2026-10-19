# Jira (Data Center)

Ingests one markdown document per issue: key fields, description, linked issues and, optionally, comments.

## 1. Create a personal access token

1. In Jira, open your profile picture → **Profile** → **Personal Access Tokens**.
2. Select **Create token** and copy it. It is shown once.

## 2. Pick a mode

| Mode | Uses |
| --- | --- |
| `project` | Project key, plus optional JQL appended as a filter |
| `jql` | The JQL query |
| `keys` | Comma-separated issue keys |

Linked-issue depth `0` ingests only matching issues; `N` follows issue links N hops. Cap the total with *Max issues*.

## 3. Test, then run

Scheduled runs are incremental (`updated >= last run`); unchanged tickets are never re-sent to LightRAG.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `401` / `403` | Wrong token, or no *Browse project* permission. |
| JQL errors | Try the query in Jira's issue search first. |
