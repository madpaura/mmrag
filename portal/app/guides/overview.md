# How ingestion works

<div class="flow" aria-label="Pipeline"><span>Source</span><span class="mut">→</span><span>Fetch</span><span class="mut">→</span><span>Skip unchanged</span><span class="mut">→</span><span>Send to LightRAG</span><span class="mut">→</span><span>Processed / failed</span></div>

1. **Add a source** (Confluence, Jira, …) from <a href="{{base}}/sources">Sources</a> and enter its configuration. Pick an update frequency.
2. **Test connection**, then **Run now**. The portal fetches content through the connector, skips anything unchanged since the last run, and sends the rest to LightRAG.
3. **Monitor progress** on the run page: found → sent → processed / failed. LightRAG does the heavy lifting (entity extraction, graph, embeddings) in the background.
4. **Manage data**: delete a single document, all data of one source, or everything.

## Direct uploads

Use <a href="{{base}}/files">Upload documents</a> for PDF, Word, Markdown or text files. Re-uploading a file with the same name replaces the old version.

## Schedules

Scheduled sources run automatically (daily or weekly) and only re-ingest changed content.

## Changed and deleted content

A changed page or issue replaces its previous version: the old version is deleted from LightRAG once its pipeline is idle (LightRAG refuses deletions while it is processing), then the new one is inserted. Content deleted at the source stays indexed until you delete it on the <a href="{{base}}/documents">Documents</a> tab.

<div class="note"><strong><svg class="i" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.25"></circle><path d="M8 7.25v4M8 4.9v.01"></path></svg>Note</strong>New connectors are plugins. See <a href="{{base}}/guides/connectors">Writing a connector</a> and the <code>mmrag_connectors</code> README.</div>
