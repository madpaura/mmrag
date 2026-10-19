import time

B = "/mmrag/t1/mgmt"


def wait_run(c, rid, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        r = c.get(f"{B}/api/runs/{rid}").json()
        if r["status"] not in ("pending", "fetching", "monitoring"):
            return r
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def add_confluence(c, **extra):
    data = {"type": "confluence", "name": "wiki", "base_url": c.atlassian + "/confluence", "token": "sim-token",
            "roots": "1000", "depth": "2", "schedule": "manual", **extra}
    r = c.post(f"{B}/sources", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].split("?")[0].rsplit("/", 1)[1])


def run(c, sid):
    r = c.post(f"{B}/sources/{sid}/run", follow_redirects=False)
    assert r.status_code == 303, r.text
    return wait_run(c, int(r.headers["location"].split("?")[0].rsplit("/", 1)[1]))


def test_pages_render_under_base_path(stack):
    for p in ("/", "/runs", "/files", "/documents", "/ask", "/guides", "/guides/confluence", "/guides/jira",
              "/sources/new?type=confluence", "/sources/new?type=jira"):
        r = stack.get(B + p)
        assert r.status_code == 200, p
    assert 'href="/mmrag/t1/mgmt/files"' in stack.get(B + "/").text
    assert stack.get("/healthz").status_code == 200


def test_validation_error_shown(stack):
    r = stack.post(f"{B}/sources", data={"type": "jira", "name": "x", "base_url": "", "token": ""})
    assert "is required" in r.text


def test_full_ingest_then_incremental_and_replace(stack):
    sid = add_confluence(stack)
    r = run(stack, sid)
    assert r["status"] == "completed" and r["discovered"] == 5 and r["processed"] == 5 and r["unchanged"] == 0
    # second run: nothing changed -> nothing sent
    r2 = run(stack, sid)
    assert r2["submitted"] == 0 and r2["unchanged"] == 5
    # change one page at the source -> exactly one replaced (delete old, insert new, no 409 failure)
    import httpx; httpx.post(stack.atlassian + "/sim/touch/page/1001").raise_for_status()
    # mock content is identical except revision text, so hash changes
    r3 = run(stack, sid)
    assert r3["submitted"] == 1 and r3["deleted"] == 1 and r3["failed"] == 0 and r3["status"] == "completed"
    time.sleep(0.5)
    assert len(stack.lightrag.DOCS) == 5


def test_jira_source_and_purge(stack):
    stack.post(f"{B}/sources", data={"type": "jira", "name": "j", "base_url": stack.atlassian + "/jira",
                                      "token": "sim-token", "mode": "project", "project": "SIM", "include_comments": "on",
                                      "max_issues": "100", "link_depth": "0", "schedule": "manual"})
    sid = [s for s in stack.app.state.db.all("select * from sources") if s["type"] == "jira"][0]["id"]
    assert run(stack, sid)["processed"] == 12
    stack.post(f"{B}/sources/{sid}/purge")
    end = time.time() + 5
    while stack.lightrag.DOCS and time.time() < end:  # deletion completes in the background
        time.sleep(0.1)
    assert len(stack.lightrag.DOCS) == 0
    assert stack.app.state.db.all("select * from documents") == []


def test_failed_document_reported(stack):
    stack.lightrag.DOCS.clear()
    files = {"files": ("bad.txt", b"please FAIL_ME", "text/plain")}
    r = stack.post(f"{B}/files", files=files, follow_redirects=False)
    res = wait_run(stack, int(r.headers["location"].split("?")[0].rsplit("/", 1)[1]))
    assert res["status"] == "completed_with_errors" and res["failed"] == 1


def test_file_upload_and_replace(stack):
    for body in (b"v1 text", b"v2 text changed"):
        r = stack.post(f"{B}/files", files={"files": ("notes.md", body, "text/markdown")}, follow_redirects=False)
        res = wait_run(stack, int(r.headers["location"].split("?")[0].rsplit("/", 1)[1]))
        assert res["status"] == "completed" and res["failed"] == 0
    end = time.time() + 5
    while len(stack.lightrag.DOCS) > 1 and time.time() < end:
        time.sleep(0.1)
    assert [d["text"] for d in stack.lightrag.DOCS.values()] == ["v2 text changed"]


def test_scheduler_runs_due_source(stack):
    sid = add_confluence(stack, schedule="daily")
    stack.app.state.db.update("sources", sid, next_run_at="2000-01-01T00:00:00Z")
    end = time.time() + 15
    while time.time() < end and not stack.app.state.db.all("select * from runs where trigger='schedule'"):
        time.sleep(0.2)
    rid = stack.app.state.db.all("select id from runs where trigger='schedule'")[0]["id"]
    assert wait_run(stack, rid)["status"] == "completed"
    assert stack.app.state.db.source(sid)["next_run_at"] > "2001"


def test_clear_requires_confirmation(stack):
    assert stack.post(f"{B}/admin/clear", data={"confirm": "nope"}).status_code == 400
    assert stack.post(f"{B}/admin/clear", data={"confirm": "t1"}, follow_redirects=False).status_code == 303


def test_changed_doc_replaced_after_pipeline_idle(stack):
    """Real LightRAG refuses deletes while busy; replacement must wait, then delete + re-insert."""
    import httpx
    sid = add_confluence(stack)
    run(stack, sid)
    # make lightrag busy with something else while the changed page is re-ingested
    stack.post(f"{B}/files", files={"files": ("busy.txt", b"x", "text/plain")})
    httpx.post(stack.atlassian + "/sim/touch/page/1002").raise_for_status()
    r = run(stack, sid)
    assert (r["status"], r["deleted"], r["submitted"], r["failed"]) == ("completed", 1, 1, 0)


def test_delete_while_busy_shows_message(stack):
    stack.post(f"{B}/files", files={"files": ("a.txt", b"abc", "text/plain")})
    end = time.time() + 5
    while not (rows := stack.app.state.db.all("select id from documents where track_id is not null")) and time.time() < end:
        time.sleep(0.02)
    pk = rows[0]["id"]
    r = stack.post(f"{B}/documents/{pk}/delete")
    assert "busy" in r.text.lower()


def test_purge_refused_during_run(stack):
    sid = add_confluence(stack)
    stack.post(f"{B}/sources/{sid}/run")
    assert stack.post(f"{B}/sources/{sid}/purge").status_code == 409


def test_untracked_document_in_lightrag_is_adopted(stack):
    """Portal DB lost / out of sync: LightRAG answers 409 -> find the doc by name and replace it."""
    from mmrag_connectors import LightRAGClient
    lr = LightRAGClient(stack.app.state.settings.lightrag_url)
    lr.insert_text("stale", "confluence_SIM_Simulated Home__1000.md")
    time.sleep(0.5)
    sid = add_confluence(stack, depth="1")
    r = run(stack, sid)
    assert (r["status"], r["deleted"], r["submitted"], r["failed"]) == ("completed", 1, 1, 0)


# ---- redesigned UI: every screen + its actions ---------------------------------------------------

def _id(resp):
    return int(resp.headers["location"].split("?")[0].rsplit("/", 1)[1])


def test_all_screens_render_with_design_shell(stack):
    sid = add_confluence(stack)
    rid = run(stack, sid)["id"]
    for p in ("/", "/sources", f"/sources/{sid}", f"/sources/{sid}?view=docs", f"/sources/{sid}?view=config",
              f"/sources/{sid}/edit", "/sources/new", "/sources/new?type=jira", "/runs", f"/runs?source={sid}",
              "/runs?status=completed", f"/runs/{rid}", "/documents", "/documents?status=failed", "/files", "/ask",
              "/guides", "/guides/confluence", "/guides/jira", "/guides/connectors", "/settings"):
        r = stack.get(B + p)
        assert r.status_code == 200, p
        assert 'class="app"' in r.text and 'class="tabs"' in r.text and "LightRAG idle" in r.text, p
    # active tab + counts
    r = stack.get(B + "/runs")
    assert 'class="tab on" href="/mmrag/t1/mgmt/runs"' in r.text
    assert '<span class="cnt">1</span>' in r.text


def test_new_source_form_layout_and_create_and_run(stack):
    r = stack.get(f"{B}/sources/new?type=jira")
    for label in ("Connector", "Connection", "What to ingest", "Update frequency", "Linked-issue depth", "Create and run now"):
        assert label in r.text
    r = stack.post(f"{B}/sources", data={"type": "jira", "name": "fw", "base_url": stack.atlassian + "/jira",
                                         "token": "sim-token", "mode": "keys", "keys": "SIM-1, SIM-2",
                                         "max_issues": "10", "link_depth": "0", "schedule": "weekly", "run_now": "1"},
                   follow_redirects=False)
    assert "/runs/" in r.headers["location"]
    res = wait_run(stack, _id(r))
    assert res["processed"] == 2
    src = [x for x in stack.app.state.db.all("select * from sources") if x["name"] == "fw"][0]
    assert src["schedule"] == "weekly" and src["next_run_at"]


def test_test_connection_unsaved_and_saved(stack):
    good = stack.post(f"{B}/sources/test", data={"type": "confluence", "base_url": stack.atlassian + "/confluence",
                                                 "token": "sim-token"})
    assert "Connected" in good.text
    bad = stack.post(f"{B}/sources/test", data={"type": "confluence", "base_url": stack.atlassian + "/confluence",
                                                "token": "nope"})
    assert "denied" in bad.text
    sid = add_confluence(stack)
    # editing: blank token means "use the stored one"
    again = stack.post(f"{B}/sources/test", data={"type": "confluence", "source_id": str(sid),
                                                  "base_url": stack.atlassian + "/confluence", "token": ""})
    assert "Connected" in again.text
    assert "Connected" in stack.post(f"{B}/sources/{sid}/test").text


def test_edit_keeps_secret_and_pauses(stack):
    sid = add_confluence(stack)
    r = stack.post(f"{B}/sources/{sid}/edit", data={"name": "wiki2", "base_url": stack.atlassian + "/confluence",
                                                    "token": "", "roots": "1000", "depth": "1", "schedule": "daily"},
                   follow_redirects=False)
    assert r.status_code == 303
    src = stack.app.state.db.source(sid)
    assert src["name"] == "wiki2" and src["config"]["token"] == "sim-token" and src["enabled"] == 0
    assert src["config"]["verify_ssl"] == ""  # unticked checkbox
    page = stack.get(f"{B}/sources/{sid}?view=config").text
    assert "sim-token" not in page and "(stored)" in page and "Schedule paused" in page


def test_bulk_delete_and_reingest(stack):
    sid = add_confluence(stack)
    run(stack, sid)
    ids = [d["id"] for d in stack.app.state.db.all("select id from documents order by id")]
    r = stack.post(f"{B}/documents/bulk", data={"ids": [str(i) for i in ids[:2]], "action": "reingest"},
                   follow_redirects=False)
    assert "Re-ingesting" in r.headers["location"].replace("%20", " ")
    rid = stack.app.state.db.one("select max(id) m from runs")["m"]
    res = wait_run(stack, rid)
    assert res["submitted"] == 2 and res["unchanged"] == 3 and res["failed"] == 0
    time.sleep(0.5)
    r = stack.post(f"{B}/documents/bulk", data={"ids": [str(i) for i in ids[:3]], "action": "delete"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert stack.app.state.db.one("select count(*) n from documents")["n"] == 2


def test_staged_upload_then_ingest(stack):
    r = stack.post(f"{B}/files", data={"ingest": "0"}, files={"files": ("later.md", b"# later", "text/markdown")},
                   follow_redirects=False)
    assert "staged" in r.headers["location"]
    page = stack.get(f"{B}/files").text
    assert "Uploaded, not ingested yet" in page and "later.md" in page
    r = stack.post(f"{B}/files/staged/ingest", follow_redirects=False)
    res = wait_run(stack, _id(r))
    assert res["processed"] == 1
    assert "Uploaded, not ingested yet" not in stack.get(f"{B}/files").text


def test_ask_shows_answer_and_references(stack):
    stack.post(f"{B}/files", files={"files": ("facts.md", b"watchdog interval is 40 s", "text/markdown")})
    time.sleep(1)
    r = stack.post(f"{B}/ask", data={"q": "what is the watchdog interval", "mode": "naive"})
    assert "<strong>[mock]</strong>" in r.text and "References" in r.text and "facts.md" in r.text
    assert 'class="on" data-mode="naive"' in r.text


def test_settings_actions(stack):
    sid = add_confluence(stack)
    run(stack, sid)
    time.sleep(0.5)
    page = stack.get(f"{B}/settings").text
    assert "wiki · 5 docs" in page and 'data-confirm-value="t1"' in page
    r = stack.post(f"{B}/sources/{sid}/purge", data={"back": "/settings"}, follow_redirects=False)
    assert r.headers["location"].startswith(f"{B}/settings?flash=Deleted%205")


def test_runs_filters(stack):
    sid = add_confluence(stack)
    run(stack, sid)
    assert "No runs match" in stack.get(f"{B}/runs?status=failed").text
    assert "wiki · Completed" in stack.get(f"{B}/runs?status=completed&source={sid}").text
    r = stack.post(f"{B}/runs/1/rerun", follow_redirects=False)
    assert wait_run(stack, _id(r))["unchanged"] == 5


def test_error_pages_use_design(stack):
    r = stack.get(f"{B}/sources/999")
    assert r.status_code == 404 and 'class="app"' in r.text
    r = stack.get(f"{B}/guides/nope")
    assert r.status_code == 404 and "unknown connector" in r.text


def test_document_view_shows_stored_content(stack):
    sid = add_confluence(stack, depth="1")
    run(stack, sid)
    d = stack.app.state.db.one("select * from documents where source_id=?", sid)
    page = stack.get(f"{B}/documents/{d['id']}").text
    assert "Content sent to LightRAG" in page and "<h1>Simulated Home</h1>" in page and "| retry |" not in page
    assert "<table>" in page  # markdown table rendered
    raw = stack.get(f"{B}/documents/{d['id']}/raw")
    assert raw.status_code == 200 and raw.text.startswith("# Simulated Home")
    assert f'href="{B}/documents/{d["id"]}"' in stack.get(f"{B}/documents").text
    # uploaded binary-ish file: download offered
    r = stack.post(f"{B}/files", files={"files": ("deck.pdf", b"%PDF-1.4 fake", "application/pdf")}, follow_redirects=False)
    wait_run(stack, _id(r))
    pdf = stack.app.state.db.one("select * from documents where title='deck.pdf'")
    assert "can't preview this format" in stack.get(f"{B}/documents/{pdf['id']}").text
    assert stack.get(f"{B}/documents/{pdf['id']}/raw?download=1").content == b"%PDF-1.4 fake"
    # deleting removes the stored copy
    stack.post(f"{B}/documents/{d['id']}/delete")
    assert stack.get(f"{B}/documents/{d['id']}/raw").status_code == 404


def test_same_item_from_two_sources_is_owned_by_the_first(stack):
    """A Jira ticket ingested by a Jira source and again via Confluence links must not be stolen/duplicated."""
    stack.post(f"{B}/sources", data={"type": "jira", "name": "issues", "base_url": stack.atlassian + "/jira",
                                      "token": "sim-token", "mode": "keys", "keys": "SIM-1", "max_issues": "10",
                                      "link_depth": "0", "schedule": "manual"})
    jid = [s for s in stack.app.state.db.all("select * from sources") if s["name"] == "issues"][0]["id"]
    assert run(stack, jid)["processed"] == 1
    time.sleep(0.5)
    sid = add_confluence(stack, depth="1", follow_jira="on", jira_instances=f"{stack.atlassian}/jira|sim-token")
    r = run(stack, sid)  # page 1000 links SIM-1 -> already owned by 'issues'
    assert r["failed"] == 0 and r["deleted"] == 0 and r["unchanged"] == 1, r
    rows = stack.app.state.db.all("select source_id from documents where file_source like '%SIM-1.md'")
    assert [x["source_id"] for x in rows] == [jid]
    log = " ".join(x["msg"] for x in stack.app.state.db.all("select msg from run_log where run_id=?", r["id"]))
    assert "already ingested by source 'issues'" in log
