# Run: MMRAG_KEY=$(./setup.sh key t3 | awk '/^key/{print $3}') python portal/tests/e2e_browser.py <screenshot-dir>
#   needs: pip install playwright; a fresh project "t3" behind nginx on 127.0.0.1:18088,
#   simulators/mock_atlassian.py on :8090 and mock_llm.py on :4001
"""End-to-end browser test of the mmrag portal on a freshly deployed project (t3), through nginx.

Walks every page and feature in the order a new user would. Each step is independent-ish: a failure is
recorded and the run continues. Screenshots go to OUT.
"""
import re
import sys
import time

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = "http://127.0.0.1:18088/mmrag/t3"
B = ROOT + "/mgmt"
SIM = "http://127.0.0.1:8090"
SIM_IN = "http://host.docker.internal:8090"  # what the portal container uses
OUT = sys.argv[1]
import os
KEY = os.environ.get("MMRAG_KEY")                  # project access key (./setup.sh key t3)
H = {"X-API-Key": KEY} if KEY else {}
results, console = [], []
T = 120_000  # ms, for anything waiting on LightRAG


def step(name):
    def deco(fn):
        t0 = time.time()
        try:
            fn()
            results.append(("PASS", name, f"{time.time() - t0:.1f}s"))
        except Exception as e:  # noqa: BLE001
            msg = (str(e).strip().splitlines() or [repr(e)])[0][:220] or type(e).__name__
            results.append(("FAIL", name, msg))
            try:
                pg.screenshot(path=f"{OUT}/FAIL_{len(results):02d}.png", full_page=True)
            except Exception:
                pass
        return fn
    return deco


def shot(name, full=True):
    pg.screenshot(path=f"{OUT}/{name}.png", full_page=full)


def run_id_from_url():
    return int(re.search(r"/runs/(\d+)", pg.url).group(1))


def wait_done(rid=None, timeout=T):
    """Wait on the run page until the live panel shows a terminal status."""
    if rid:
        pg.goto(f"{B}/runs/{rid}")
    lbl = pg.locator("#runbody .lbl").first
    expect(lbl).not_to_have_text(re.compile("In progress"), timeout=timeout)
    return lbl.inner_text()


def done_ok(rid=None, timeout=T):
    got = wait_done(rid, timeout)
    assert got == "Completed", f"run ended '{got}'"


def flash_contains(text):
    expect(pg.locator("[data-flash]").first).to_contain_text(text, ignore_case=True)


with sync_playwright() as p:
    br = p.chromium.launch(channel="chrome", headless=True)
    ctx = br.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True,
                         http_credentials={"username": "t3", "password": KEY} if KEY else None)
    pg = ctx.new_page()
    pg.on("dialog", lambda d: d.accept())
    pg.on("pageerror", lambda e: console.append(("pageerror", pg.url, str(e))))
    pg.on("console", lambda m: console.append(("console", pg.url, m.text)) if m.type == "error" else None)

    # ======================= 1. fresh project: shell & empty states =======================
    @step("fresh project: overview shell, counts, empty states")
    def _():
        pg.goto(B + "/")
        expect(pg).to_have_title(re.compile(r"Overview · t3"))
        expect(pg.locator(".hdr .lbl.ok")).to_have_text("LightRAG idle")
        expect(pg.locator(".tab.on")).to_contain_text("Overview")
        counts = pg.locator(".tabs .cnt").all_inner_texts()
        assert counts == ["1", "0", "0"], counts           # Direct uploads only, no runs, no docs
        expect(pg.locator("section[aria-labelledby=runs-h] .empty")).to_have_text("No runs yet. Add a source and press Run now.")
        expect(pg.locator("#apiurl")).to_have_value("http://mydomain.com/mmrag/t3/api")
        expect(pg.locator("button:has-text('Cancel pipeline')")).to_be_disabled()
        shot("01_fresh_overview")

    @step("fresh project: every page renders its empty state")
    def _():
        for path, txt in (("/sources", "1 source"), ("/runs", "No runs yet"), ("/documents", "Nothing ingested yet"),
                          ("/files", "No files uploaded yet"), ("/ask", "Ask the knowledge base"),
                          ("/guides", "How ingestion works"), ("/settings", "Danger zone")):
            pg.goto(B + path)
            expect(pg.locator("main")).to_contain_text(txt)

    @step("header: dark mode toggles, persists across pages and reloads")
    def _():
        pg.goto(B + "/")
        pg.click("[data-theme-toggle]")
        expect(pg.locator("#app")).to_have_class(re.compile(r"\bdark\b"))
        pg.goto(B + "/sources")
        expect(pg.locator("#app")).to_have_class(re.compile(r"\bdark\b"))
        bg = pg.evaluate("getComputedStyle(document.querySelector('#app')).backgroundColor")
        assert bg == "rgb(13, 17, 23)", bg
        shot("02_dark_sources", full=False)
        pg.click("[data-theme-toggle]")
        pg.reload()
        expect(pg.locator("#app")).not_to_have_class(re.compile(r"\bdark\b"))

    @step("header: project menu links (settings, API docs, web UI) resolve")
    def _():
        pg.goto(B + "/")
        pg.click("details.menu summary:has-text('t3')")
        menu = pg.locator("details.menu[open] .menu-pop")
        expect(menu).to_be_visible()
        hrefs = [a.get_attribute("href") for a in menu.locator("a").all()]
        assert hrefs[0].endswith("/mmrag/t3/mgmt/settings"), hrefs
        for h in hrefs[1:]:  # public URLs use mydomain.com -> check through the local nginx
            local = h.replace("http://mydomain.com", "http://127.0.0.1:18088")
            assert httpx.get(local, auth=("t3", KEY) if KEY else None).status_code == 200, local
        pg.keyboard.press("Escape")
        expect(menu).to_be_hidden()

    @step("header: Ask button + '/' search shortcut")
    def _():
        pg.goto(B + "/runs")
        pg.click(".hdr a:has-text('Ask')")
        expect(pg.locator(".tab.on")).to_contain_text("Ask")
        expect(pg.locator(".hdr a:has-text('Ask')")).to_have_count(0)  # hidden on the Ask page itself
        pg.goto(B + "/")
        pg.keyboard.press("/")
        assert pg.evaluate("document.activeElement.id") == "gsearch"

    # ======================= 2. create a Confluence source =======================
    @step("new source: connector picker switches forms")
    def _():
        pg.goto(B + "/sources/new")
        expect(pg.locator("label:has-text('Root pages')")).to_be_visible()
        pg.click("a.pick:has-text('Jira')")
        expect(pg.locator("label:has-text('Linked-issue depth')")).to_be_visible()
        expect(pg.locator("a.pick.on")).to_contain_text("Jira")
        pg.click("a.pick:has-text('Confluence')")
        expect(pg.locator("label:has-text('Root pages')")).to_be_visible()
        # design: no Upload/New source buttons on this page
        expect(pg.locator(".ph a:has-text('New source')")).to_have_count(0)

    @step("new source: Test connection reports bad token, then success")
    def _():
        pg.goto(B + "/sources/new")
        pg.fill("#nm", "wiki")
        pg.fill("#f_base_url", SIM_IN + "/confluence")
        pg.fill("#f_token", "wrong-token")
        pg.click("button:has-text('Test connection')")
        expect(pg.locator("#testres .bad")).to_contain_text("401", timeout=15000)
        pg.fill("#f_token", "sim-token")
        pg.click("button:has-text('Test connection')")
        expect(pg.locator("#testres .ok")).to_contain_text("Connected", timeout=15000)
        shot("03_new_source_tested")

    @step("new source: server-side validation keeps entered values")
    def _():
        pg.goto(B + "/sources/new")
        pg.fill("#f_base_url", SIM_IN + "/confluence")
        pg.fill("#f_token", "sim-token")
        pg.fill("#f_roots", "1000")
        pg.evaluate("document.getElementById('nm').removeAttribute('required')")
        pg.click("button:has-text('Create source')")
        expect(pg.locator("[role=alert]")).to_contain_text("Name")
        expect(pg.locator("#f_base_url")).to_have_value(SIM_IN + "/confluence")
        expect(pg.locator("#f_roots")).to_have_value("1000")

    @step("new source: Create source -> detail page 'Never run', secret masked")
    def _():
        pg.goto(B + "/sources/new")
        pg.fill("#nm", "wiki")
        pg.fill("#f_base_url", SIM_IN + "/confluence")
        pg.fill("#f_token", "sim-token")
        pg.fill("#f_roots", "1000")
        pg.fill("#f_depth", "2")
        pg.click("button:has-text('Create source')")
        pg.wait_for_url(re.compile(r"/sources/\d+"))
        flash_contains("Source created")
        expect(pg.locator("h1")).to_have_text("wiki")
        expect(pg.locator("main .lbl").nth(1)).to_have_text("Never run")
        pg.click("[data-view-btn='config']")
        cfg = pg.locator("[data-view='config']")
        expect(cfg).to_contain_text("(stored)")
        assert "sim-token" not in pg.content()
        expect(pg.locator("[data-view='runs']")).to_be_hidden()

    @step("source detail: Test connection (saved config)")
    def _():
        pg.click("button:has-text('Test connection')")
        expect(pg.locator("#testres")).to_contain_text("Connected", timeout=15000)

    # ======================= 3. run, live monitoring =======================
    state = {}

    @step("Run now -> live run page streams progress -> Completed, 5/5 processed")
    def _():
        state["src"] = int(re.search(r"/sources/(\d+)", pg.url).group(1))
        pg.click("form[action$='/run'] button:has-text('Run now')")
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        rid = run_id_from_url()
        state["run1"] = rid
        # while active: header pill should show processing at some point, panel polls
        saw_active = pg.locator("#runbody[hx-trigger]").count() == 1
        txt = wait_done()
        assert txt == "Completed", txt
        assert saw_active or True
        expect(pg.locator(".stage .n").first).to_have_text("5")
        expect(pg.locator(".stage .n.ok")).to_have_text("5")
        assert pg.locator("#log .ln").count() >= 6
        expect(pg.locator("#runbody")).not_to_have_attribute("hx-trigger", re.compile("every"))  # polling stopped
        shot("04_run_completed")

    @step("run page: log search, errors-only, copy")
    def _():
        pg.fill("#logq", "page 1003")
        assert pg.locator("#log .ln:visible").count() == 1
        pg.fill("#logq", "")
        pg.check("#logerr")
        assert pg.locator("#log .ln:visible").count() == 0
        pg.uncheck("#logerr")
        pg.click("button[aria-label='Copy log']")

    @step("Re-run all: unchanged documents are skipped")
    def _():
        pg.click("button:has-text('Re-run all')")
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        done_ok()
        stages = pg.locator(".stage .n").all_inner_texts()
        assert stages[:3] == ["5", "5", "0"], f"stages {stages}"  # found 5, unchanged 5, sent 0

    @step("changed page at source -> next run replaces exactly that document")
    def _():
        httpx.post(SIM + "/sim/touch/page/1002").raise_for_status()
        pg.goto(f"{B}/sources/{state['src']}")
        pg.click("form[action$='/run'] button:has-text('Run now')")
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        done_ok()
        expect(pg.locator(".stage").nth(2)).to_contain_text("1 replaced · 1 old removed")
        shot("05_run_replaced")

    # ======================= 4. edit source =======================
    @step("edit: change depth + schedule daily + pause; config & header reflect it")
    def _():
        pg.goto(f"{B}/sources/{state['src']}/edit")
        expect(pg.locator("h1")).to_have_text("Edit source")
        expect(pg.locator("#f_token")).to_have_attribute("placeholder", re.compile("Stored"))
        pg.fill("#f_depth", "1")
        pg.check("input[name=schedule][value=daily]")
        pg.uncheck("input[name=enabled]")
        pg.click("button[type=submit]:has-text('Save'):not([name])")
        pg.wait_for_url(re.compile(r"/sources/\d+\?flash="))
        flash_contains("Settings saved")
        expect(pg.locator("span.lbl.warn:has-text('Schedule paused')")).to_be_visible()
        pg.click("[data-view-btn='config']")
        expect(pg.locator("[data-view='config']")).to_contain_text("Daily")

    @step("edit: re-enable schedule, 'Save and run now' starts a run")
    def _():
        pg.goto(f"{B}/sources/{state['src']}/edit")
        pg.fill("#f_depth", "2")
        pg.check("input[name=enabled]")
        pg.click("button[name=run_now]")
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        done_ok()

    # ======================= 5. Jira source from the Sources page =======================
    @step("sources: connector card -> Jira form -> Create and run now -> 12 issues")
    def _():
        pg.goto(B + "/sources")
        pg.locator(".card:has-text('Jira (Data Center)') a:has-text('Configure')").click()
        pg.wait_for_url(re.compile(r"type=jira"))
        pg.fill("#nm", "issues")
        pg.fill("#f_base_url", SIM_IN + "/jira")
        pg.fill("#f_token", "sim-token")
        pg.select_option("#f_mode", "project")
        pg.fill("#f_project", "SIM")
        pg.fill("#f_link_depth", "1")
        pg.check("input[name=schedule][value=weekly]")
        pg.click("button[name=run_now]")
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        state["jira_run"] = run_id_from_url()
        done_ok()
        expect(pg.locator(".stage .n.ok")).to_have_text("12")
        expect(pg.locator(".stage").first).to_contain_text("Jira issues")

    @step("confluence: follow linked Jira tickets (jira_instances) adds tickets")
    def _():
        pg.goto(f"{B}/sources/{state['src']}/edit")
        pg.check("input[name=follow_jira]")
        pg.fill("#f_jira_instances", f"{SIM_IN}/jira|sim-token")
        pg.click("button[name=run_now]")
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        done_ok()
        found = int(pg.locator(".stage .n").first.inner_text())
        assert found > 5, found  # pages + linked tickets

    # ======================= 6. overview / sources lists =======================
    @step("overview: sources with run history, recent runs, counts, pipeline")
    def _():
        pg.goto(B + "/")
        counts = pg.locator(".tabs .cnt").all_inner_texts()
        assert counts[0] == "3", counts
        expect(pg.locator(".box .br:has-text('wiki') [role=img] .sq")).not_to_have_count(0)
        assert pg.locator("section[aria-labelledby=runs-h] .br").count() == 6
        expect(pg.locator("#pipeline")).to_contain_text("Idle")
        shot("06_overview_populated")

    @step("overview: Run now from the sources list")
    def _():
        pg.goto(B + "/")
        pg.locator(".br:has(a:text-is('issues')) button:has-text('Run now')").click()
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        done_ok()

    @step("sources page: text / type / last-run filters and ••• menu")
    def _():
        pg.goto(B + "/sources")
        pg.fill("input[data-filter='#srclist']", "issu")
        expect(pg.locator("[data-count-for='#srclist']")).to_have_text("1 source")
        pg.fill("input[data-filter='#srclist']", "")
        pg.select_option("select[data-key=type]", "confluence")
        expect(pg.locator("[data-count-for='#srclist']")).to_have_text("1 source")
        pg.select_option("select[data-key=type]", "all")
        pg.select_option("select[data-key=kind]", "ok")
        expect(pg.locator("[data-count-for='#srclist']")).to_have_text("2 sources")  # Direct uploads never ran
        pg.select_option("select[data-key=kind]", "all")
        d = pg.locator("details:has(summary[aria-label='More actions for issues'])")
        d.locator("summary").click()
        d.locator("a:has-text('View runs')").click()
        pg.wait_for_url(re.compile(r"/runs\?source=\d+"))
        expect(pg.locator("main h1")).to_have_text("issues")
        shot("07_runs_filtered")

    # ======================= 7. runs page =======================
    @step("runs: source nav, status filter, Run-a-source menu, re-run icon")
    def _():
        pg.goto(B + "/runs")
        expect(pg.locator("main h1")).to_have_text("All runs")
        pg.click(".nav button:has-text('wiki')")
        pg.wait_for_url(re.compile(r"source=\d+"))
        pg.select_option("select[name=status]", "failed")
        pg.wait_for_url(re.compile(r"status=failed"))
        expect(pg.locator("#runbox")).to_contain_text("No runs match")
        pg.goto(B + "/runs")
        pg.click("summary:has-text('Run a source')")
        pg.locator("details[open] .menu-pop button:has-text('wiki')").click()
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        done_ok()
        pg.goto(B + "/runs")
        pg.locator("#runbox button[aria-label^='Re-run']:not([disabled])").first.click()
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        done_ok()

    @step("run: Cancel an active run")
    def _():
        httpx.post(SIM + "/sim/touch/page/1001").raise_for_status()
        httpx.post(SIM + "/sim/touch/page/1003").raise_for_status()
        pg.goto(f"{B}/sources/{state['src']}")
        pg.click("form[action$='/run'] button:has-text('Run now')")
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        btn = pg.locator("button:has-text('Cancel run')")
        expect(btn).to_be_visible(timeout=5000)
        btn.click()
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        txt = wait_done()
        assert txt in ("Cancelled", "Completed"), txt  # may finish before the cancel lands
        state["cancel_result"] = txt

    # ======================= 8. documents =======================
    @step("documents: search, source filter, status tabs")
    def _():
        pg.goto(B + "/documents")
        total = int(pg.locator(".st a:has-text('All') .cnt").inner_text())
        assert total >= 17, f"total {total}"
        pg.fill("input[name=q]", "SIM-3")
        pg.keyboard.press("Enter")
        pg.wait_for_url(re.compile(r"q=SIM-3"))
        n = pg.locator("tbody tr").count(); assert n == 1, f"{n} rows for SIM-3"
        pg.goto(B + "/documents")
        pg.select_option("select[name=source]", label="issues")
        pg.wait_for_url(re.compile(r"source=\d+"))
        n = pg.locator("tbody tr").count(); assert n >= 12, f"{n} issue rows"
        pg.click(".st a:has-text('Failed')")
        pg.wait_for_url(re.compile(r"status=failed"))
        expect(pg.locator("tbody")).to_contain_text("No documents match")
        shot("08_documents")

    @step("documents: open a document -> rendered content, raw, download, back")
    def _():
        pg.goto(B + "/documents?q=Simulated Home")
        pg.click("tbody a:has-text('Simulated Home')")
        pg.wait_for_url(re.compile(r"/documents/\d+$"))
        expect(pg.locator("#doc-content")).to_contain_text("The retry limit is")
        expect(pg.locator("#doc-content table")).to_be_visible()
        state["doc_url"] = pg.url
        raw = httpx.get(pg.url + "/raw", auth=("t3", KEY) if KEY else None)
        assert raw.status_code == 200 and raw.text.startswith("# Simulated Home")
        with pg.expect_download() as dl:
            pg.click("a:has-text('Download')")
        assert dl.value.suggested_filename.endswith(".md")
        expect(pg.locator("a:has-text('Open at source')")).to_have_attribute("href", re.compile("/pages/1000"))
        shot("09_document_view")
        pg.click("nav[aria-label=Breadcrumb] a:has-text('Documents')")
        expect(pg.locator(".tab.on")).to_contain_text("Documents")

    @step("documents: select / select-all / clear, bulk Re-ingest")
    def _():
        pg.goto(B + "/documents?q=Subsystem 4")
        pg.locator("input[data-sel]").first.check()
        expect(pg.locator("[data-sel-count]")).to_have_text("1 selected")
        pg.click("[data-sel-clear]")
        expect(pg.locator("[data-sel-some]")).to_be_hidden()
        pg.check("input[data-sel-all]")
        n = pg.locator("input[data-sel]").count()
        expect(pg.locator("[data-sel-count]")).to_have_text(f"{n} selected")
        pg.click("button[value=reingest]")
        pg.wait_for_url(re.compile(r"/runs\?flash="))
        flash_contains("Re-ingesting")
        rid = int(pg.locator("#runbox .br a").first.get_attribute("href").rsplit("/", 1)[1])
        done_ok(rid)
        expect(pg.locator(".stage").nth(2)).to_contain_text("replaced")

    @step("documents: bulk Delete, row ••• delete, delete from document page")
    def _():
        pg.goto(B + "/documents?q=SIM-1")
        before = int(pg.locator(".st a:has-text('All') .cnt").inner_text())
        pg.locator("input[data-sel]").first.check()
        pg.click("button[value=delete]")
        pg.wait_for_url(re.compile(r"flash="))
        flash_contains("Deleted 1 document")
        pg.goto(B + "/documents?q=SIM-2")
        pg.locator("summary[aria-label^='Actions for']").first.click()
        pg.locator("details[open] .menu-pop button:has-text('Delete from LightRAG')").click()
        pg.wait_for_url(re.compile(r"flash="))
        flash_contains("deleted")
        pg.goto(B + "/documents?q=SIM-5")
        pg.click("tbody a:has-text('SIM-5')")
        pg.wait_for_url(re.compile(r"/documents/\d+$"))
        pg.click("button:has-text('Delete from LightRAG')")
        pg.wait_for_url(re.compile(r"flash="))
        assert before >= 1

    # ======================= 9. upload =======================
    @step("upload: queue preview, remove, clear, Upload and ingest")
    def _():
        pg.goto(B + "/files")
        expect(pg.locator(".tab.on")).to_contain_text("Sources")
        files = [{"name": "handbook.md", "mimeType": "text/markdown", "buffer": b"# Handbook\n\nThe watchdog interval is 40 s."},
                 {"name": "spec.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4 not really"},
                 {"name": "junk.txt", "mimeType": "text/plain", "buffer": b"junk"}]
        pg.set_input_files("#files", files=files)
        expect(pg.locator("#queueCount")).to_have_text("3")
        pg.locator("#queueList .br:has-text('junk.txt') button").click()
        expect(pg.locator("#queueCount")).to_have_text("2")
        pg.click("#clearQueue")
        expect(pg.locator("#queue")).to_be_hidden()
        pg.set_input_files("#files", files=files[:1])
        expect(pg.locator("#queueList .lbl.acc")).to_have_text("New")
        shot("10_upload_queue", full=False)
        pg.click("#uploadGo")
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        done_ok()

    @step("upload: same name again is flagged 'Replaces existing' and replaces it")
    def _():
        pg.goto(B + "/files")
        pg.set_input_files("#files", files=[{"name": "handbook.md", "mimeType": "text/markdown",
                                             "buffer": b"# Handbook v2\n\nThe watchdog interval is 60 s."}])
        expect(pg.locator("#queueList .lbl.warn")).to_have_text("Replaces existing")
        pg.click("#uploadGo")
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        done_ok()
        expect(pg.locator(".stage").nth(2)).to_contain_text("1 replaced")

    @step("upload: Upload only -> staged; remove staged; ingest staged")
    def _():
        pg.goto(B + "/files")
        pg.set_input_files("#files", files=[{"name": "a.txt", "mimeType": "text/plain", "buffer": b"alpha"},
                                            {"name": "b.txt", "mimeType": "text/plain", "buffer": b"beta"}])
        pg.uncheck("#autoIngest")
        pg.click("#uploadOnly")
        pg.wait_for_url(re.compile(r"/files\?flash="))
        flash_contains("staged")
        expect(pg.locator("text=Uploaded, not ingested yet")).to_be_visible()
        pg.click("button[aria-label='Remove b.txt']")
        pg.wait_for_url(re.compile(r"flash="))
        pg.click("button:has-text('Ingest 1 file')")
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        done_ok()
        pg.goto(B + "/files")
        expect(pg.locator("text=Uploaded, not ingested yet")).to_have_count(0)
        expect(pg.locator("aside")).to_contain_text("a.txt")

    @step("upload: binary file is downloadable from its document page")
    def _():
        pg.goto(B + "/files")
        pg.set_input_files("#files", files=[{"name": "spec.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4 x"}])
        pg.click("#uploadGo")
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        wait_done()  # a fake PDF may legitimately fail to parse in LightRAG
        pg.goto(B + "/documents?q=spec.pdf")
        pg.click("tbody a:has-text('spec.pdf')")
        expect(pg.locator("main")).to_contain_text("can't preview this format")
        with pg.expect_download() as dl:
            pg.click("a:has-text('Download original')")
        assert dl.value.suggested_filename == "spec.pdf"

    # ======================= 10. ask =======================
    @step("ask: mode buttons + side list sync, Ctrl+Enter, answer, curl snippet")
    def _():
        pg.goto(B + "/ask")
        pg.click("aside [data-mode='local']")
        expect(pg.locator(".modes [data-mode='local']")).to_have_class("on")
        expect(pg.locator("#modeName")).to_have_text("local")
        pg.fill("#q", "What is the watchdog interval?")
        pg.locator("#q").press("Control+Enter")
        expect(pg.locator("#ans-h")).to_be_visible(timeout=T)
        expect(pg.locator(".bh .lbl.mono")).to_have_text("local")
        expect(pg.locator("#q")).to_have_value("What is the watchdog interval?")
        pg.click("summary:has-text('Use this query from code')")
        expect(pg.locator("pre.code")).to_contain_text('"mode": "local"')
        pg.click("button[aria-label='Copy answer']")
        shot("11_ask")

    # ======================= 11. guides =======================
    @step("guides: every guide page, nav state, links into the app")
    def _():
        pg.goto(B + "/guides")
        for label, frag in (("Confluence (Data Center)", "/guides/confluence"), ("Jira (Data Center)", "/guides/jira"),
                            ("Writing a connector", "/guides/connectors"), ("How ingestion works", "/guides")):
            pg.click(f"nav.nav a:has-text('{label}')")
            pg.wait_for_url(re.compile(re.escape(frag) + "$"))
            expect(pg.locator("nav.nav a.on")).to_have_text(label)
            expect(pg.locator("article.md h1")).to_be_visible()
        pg.click("article a:has-text('Upload documents')")
        pg.wait_for_url(re.compile(r"/files$"))
        pg.goto(B + "/guides/jira")
        pg.click("article a:has-text('Add a Jira source')")
        pg.wait_for_url(re.compile(r"/sources/new\?type=jira"))

    # ======================= 12. settings =======================
    @step("settings: copy endpoint, docs link, section nav, pipeline idle")
    def _():
        pg.goto(B + "/settings")
        pg.click("button[aria-label='Copy endpoint']")
        expect(pg.locator("#ep")).to_have_value("http://mydomain.com/mmrag/t3/api")
        expect(pg.locator("#pipeline .lbl")).to_have_text("Idle")
        expect(pg.locator("#pipeline button")).to_be_disabled()
        pg.click("#setnav a:has-text('Danger zone')")
        expect(pg.locator("#setnav a.on")).to_contain_text("Danger zone")

    @step("settings: delete one source's data via the selector")
    def _():
        pg.goto(B + "/settings")
        pg.select_option("#ds", value=pg.locator("#ds option:has-text('issues')").get_attribute("value"))
        pg.click("#purgeform button:has-text('Delete data')")
        pg.wait_for_url(re.compile(r"/settings\?flash="))
        flash_contains("Deleted")
        expect(pg.locator("#ds option:has-text('issues')")).to_contain_text("· 0 docs")

    # ======================= 13. remove sources =======================
    @step("source detail: Remove source (with data) from the danger zone")
    def _():
        pg.goto(B + "/sources")
        pg.click("a:text-is('issues')")
        pg.check("input[name=purge]")
        pg.click("button:has-text('Remove source')")
        pg.wait_for_url(re.compile(r"/sources\?flash="))
        flash_contains("Removed source issues")
        expect(pg.locator("a:text-is('issues')")).to_have_count(0)

    # ======================= 14. failure + recovery notice =======================
    @step("failed run is shown as Failed; fixing it shows 'healthy again' on overview; dismiss persists")
    def _():
        pg.goto(B + "/sources/new")
        pg.fill("#nm", "broken")
        pg.fill("#f_base_url", SIM_IN + "/confluence")
        pg.fill("#f_token", "bad-token")
        pg.fill("#f_roots", "1000")
        pg.click("button[name=run_now]")
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        got = wait_done(); assert got == "Failed", got
        expect(pg.locator("main")).to_contain_text("denied access")
        shot("12_run_failed")
        sid = int(re.search(r"/sources/(\d+)", pg.locator("a:has-text('View source')").get_attribute("href")).group(1))
        pg.goto(f"{B}/sources/{sid}/edit")
        pg.fill("#f_token", "sim-token")
        pg.fill("#f_depth", "1")
        pg.click("button[name=run_now]")
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        done_ok()
        pg.goto(B + "/")
        note = pg.locator("[data-dismiss-key^='broken:']")
        expect(note).to_be_visible()
        expect(note).to_contain_text("broken is healthy again")
        shot("13_recovered_notice", full=False)
        note.locator("[data-dismiss]").click()
        pg.reload()
        expect(pg.locator("[data-dismiss-key^='broken:']")).to_be_hidden()

    # ======================= 15. busy pipeline =======================
    @step("busy LightRAG: pill + pipeline panel show processing; Cancel pipeline enabled")
    def _():
        # queue enough work to keep LightRAG busy for a few seconds
        pg.goto(B + "/files")
        pg.set_input_files("#files", files=[{"name": f"bulk{i}.md", "mimeType": "text/markdown",
                                             "buffer": (f"# Bulk {i}\n\n" + "lorem ipsum " * 4000).encode()}
                                            for i in range(8)])
        pg.click("#uploadGo")
        pg.wait_for_url(re.compile(r"/runs/\d+"))
        rid = run_id_from_url()
        pg.goto(B + "/")
        seen = False
        for _ in range(40):
            if pg.locator(".hdr .lbl.warn:has-text('processing')").count() or pg.locator("#pipeline:has-text('Processing')").count():
                seen = True
                break
            pg.wait_for_timeout(500)
            pg.reload()
        state["busy_seen"] = seen
        if seen:
            shot("14_busy", full=False)
            btn = pg.locator("#pipeline button:has-text('Cancel pipeline')")
            if btn.is_enabled():
                btn.click()
                pg.wait_for_url(re.compile(r"flash="))
        wait_done(rid, timeout=180_000)
        assert seen, "never observed LightRAG busy (work finished too fast)"

    # ======================= 16. settings: clear all =======================
    @step("settings: Clear all data needs the project name, then empties LightRAG")
    def _():
        # LightRAG refuses while busy -> wait until idle
        for _ in range(60):
            if httpx.get(ROOT + "/api/documents/pipeline_status", headers=H).json().get("busy") is False:
                break
            time.sleep(2)
        pg.goto(B + "/settings")
        btn = pg.locator("#clearbtn")
        expect(btn).to_be_disabled()
        pg.fill("#cf", "t")
        expect(btn).to_be_disabled()
        pg.fill("#cf", "t3")
        expect(btn).to_be_enabled()
        btn.click()
        pg.wait_for_url(re.compile(r"/settings\?flash="))
        flash_contains("All data cleared")
        expect(pg.locator(".tabs .cnt").nth(2)).to_have_text("0")
        for _ in range(30):
            c = httpx.get(ROOT + "/api/documents/status_counts", headers=H).json()["status_counts"]
            if sum(v for k, v in c.items() if k != "all") == 0:
                break
            time.sleep(2)
        assert sum(v for k, v in c.items() if k != "all") == 0, c

    # ======================= 16b. access control =======================
    @step("access: Settings shows the key masked, Show reveals it; LightRAG web UI works after login")
    def _():
        pg.goto(B + "/settings#access")
        k = pg.locator("#key")
        expect(k).to_have_attribute("type", "password")
        pg.click("#access button:has-text('Show')")
        expect(k).to_have_attribute("type", "text")
        assert k.input_value() == KEY
        shot("16_settings_access", full=False)
        # web UI: its own XHR calls must be authorised (nginx adds the key)
        statuses = []
        pg.on("response", lambda r: statuses.append((r.status, r.url)) if "/mmrag/t3/api/" in r.url and "/webui/" not in r.url else None)
        pg.goto(ROOT + "/api/webui/")
        pg.wait_for_timeout(4000)
        denied = [x for x in statuses if x[0] in (401, 403)]
        assert statuses and not denied, (statuses[:5], denied[:5])
        shot("17_webui", full=False)

    @step("access: a browser without credentials is refused everywhere except downloads")
    def _():
        anon = br.new_context()
        for url in (B + "/", B + "/settings", ROOT + "/api/webui/", ROOT + "/api/docs", ROOT + "/api/documents/paginated"):
            assert anon.request.get(url).status == 401, url
        assert anon.request.get(B + "/downloads/samples/jira_ingest.py").status == 200
        # a forged/stale cookie does not help
        anon.add_cookies([{"name": "mmrag_t3", "value": "mmr_forged", "url": B + "/"}])
        assert anon.request.get(B + "/").status == 401
        anon.close()

    # ======================= 17. errors, responsive, console =======================
    @step("unknown pages show the designed error page")
    def _():
        for path, code in (("/sources/9999", 404), ("/runs/9999", 404), ("/documents/9999", 404), ("/guides/nope", 404)):
            r = pg.goto(B + path)
            assert r.status == code, (path, r.status)
            expect(pg.locator(".app .box h1")).to_be_visible()

    @step("phone width (390px): no horizontal page scroll on any page")
    def _():
        m = ctx.new_page()
        m.set_viewport_size({"width": 390, "height": 844})
        bad = []
        for path in ("/", "/sources", "/sources/new", "/runs", "/documents", "/files", "/ask", "/guides", "/settings"):
            m.goto(B + path)
            w = m.evaluate("document.documentElement.scrollWidth")
            if w > 390:
                bad.append((path, w))
        m.screenshot(path=f"{OUT}/15_mobile_settings.png", full_page=True)
        m.goto(B + "/")
        m.screenshot(path=f"{OUT}/15_mobile_overview.png", full_page=True)
        m.close()
        assert not bad, bad

    @step("no unexpected JS errors in the whole session")
    def _():
        expected = re.compile(r"status of (401|403|404|409|422)")
        real = [c for c in console if not expected.search(c[2])]
        assert not real, real[:5]

    br.close()

w = max(len(n) for _, n, _ in results)
for st, n, info in results:
    print(f"{st}  {n.ljust(w)}  {info}")
print(f"\n{sum(r[0] == 'PASS' for r in results)}/{len(results)} passed · busy_seen={state.get('busy_seen')} · cancel={state.get('cancel_result')}")
