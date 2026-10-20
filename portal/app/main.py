from __future__ import annotations

import html
import json
import os
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import markdown
from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mmrag_connectors import ConnectorError, LightRAGBusy, LightRAGError, available, get

from . import views as v
from .config import Settings
from .db import ACTIVE, DB, now
from .ingest import Runner, Scheduler, next_run

HERE = Path(__file__).parent
GUIDE_FILES = {"overview": "overview.md", "connectors": "writing-connectors.md"}


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or Settings()
    Path(s.data_dir, "uploads").mkdir(parents=True, exist_ok=True)
    staged_dir = Path(s.data_dir, "staged")
    staged_dir.mkdir(parents=True, exist_ok=True)
    db = DB(str(Path(s.data_dir, "portal.db")))
    runner = Runner(db, s)
    sched = Scheduler(db, runner, s.scheduler_tick)
    # the "Direct uploads" pseudo-source
    if not db.one("select 1 x from sources where type='files'"):
        db.run("insert into sources(name,type,config,schedule,created_at) values ('Direct uploads','files','{}','manual',?)", now())

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        sched.start()
        yield
        sched.stop()
        runner.shutdown()

    app = FastAPI(title=f"mmrag portal ({s.project})", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    tpl = Jinja2Templates(directory=str(HERE / "templates"))
    tpl.env.add_extension("jinja2.ext.loopcontrols")
    api_url = (s.public_api_url or s.lightrag_url).rstrip("/")
    tpl.env.globals.update(base=s.base_path, project=s.project, api_url=api_url, v=v)
    for f in ("fmt_short", "fmt_sec", "fmt_full", "fmt_time"):
        tpl.env.filters[f] = getattr(v, f)
    r = APIRouter()
    app.state.db, app.state.runner, app.state.settings = db, runner, s

    _stats_cache: dict = {"t": 0.0, "v": None}

    def lr_stats(fresh: bool = False) -> dict:
        if not fresh and _stats_cache["v"] and time.time() - _stats_cache["t"] < 2:
            return _stats_cache["v"]
        lr = runner.lr()
        try:
            counts = {k.lower(): n for k, n in lr.status_counts().items()}
            pipe = lr.pipeline_status()
            st = {"ok": True, "counts": counts, "pipeline": pipe, "busy": bool(pipe.get("busy")),
                  "active": sum(n for k, n in counts.items() if k in ("pending", "processing", "preprocessed", "analyzing")),
                  "failed": counts.get("failed", 0), "processed": counts.get("processed", 0)}
        except LightRAGError as e:
            st = {"ok": False, "error": str(e), "counts": {}, "pipeline": {}, "busy": False, "active": 0, "failed": 0,
                  "processed": 0}
        finally:
            lr.close()
        _stats_cache.update(t=time.time(), v=st)
        return st

    def nav() -> dict:
        return {"sources": db.one("select count(*) n from sources")["n"],
                "runs": db.one("select count(*) n from runs")["n"],
                "docs": db.one("select count(*) n from documents")["n"]}

    def page(request: Request, template: str, tab: str = "", **ctx):
        ctx.setdefault("stats", lr_stats())
        flash = request.query_params.get("flash")
        return tpl.TemplateResponse(request, template, {"tab": tab, "nav": nav(), "flash": flash,
                                                    "flash_kind": request.query_params.get("fk", "ok"), **ctx})

    def go(path: str, flash: str | None = None, kind: str = "ok"):
        if flash:
            path += ("&" if "?" in path else "?") + f"flash={quote(flash)}&fk={kind}"
        return RedirectResponse(f"{s.base_path}{path}", status_code=303)

    def files_source() -> dict:
        return db.source(db.one("select id from sources where type='files'")["id"])

    def source_runs(sid: int, n: int = 8) -> list[dict]:
        return db.all("select * from runs where source_id=? order by id desc limit ?", sid, n)

    def source_card(src: dict) -> dict:
        runs = source_runs(src["id"])
        h = v.hist(runs)
        last = v.run_view(runs[0]) if runs else None
        docs = db.one("select count(*) n from documents where source_id=?", src["id"])["n"]
        label, cls = v.health(runs)
        return {**src, "type_label": v.TYPE_LABEL.get(src["type"], src["type"].title()),
                "desc": v.source_desc(src), "docs": docs, "hist": h, "hist_label": v.hist_label(h), "last": last,
                "health": label, "health_cls": cls, "is_files": src["type"] == "files",
                "href": "/files" if src["type"] == "files" else f"/sources/{src['id']}"}

    def all_cards() -> list[dict]:
        rows = db.all("select id from sources order by id")
        cards = [source_card(db.source(x["id"])) for x in rows]
        return sorted(cards, key=lambda c: c["last_run_at"] or "", reverse=True)

    def runs_with_source(where: str = "", *args, limit: int = 100) -> list[dict]:
        rows = db.all("select r.*, s.name sname, s.type stype from runs r join sources s on s.id=r.source_id "
                      f"{where} order by r.id desc limit {int(limit)}", *args)
        return [v.run_view(x) for x in rows]

    def connectors() -> dict:
        return available()

    # ---- overview -------------------------------------------------------
    @r.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        cards = all_cards()
        notices = []
        for c in cards:
            n = v.recovery(c["name"], source_runs(c["id"]))
            if n:
                n["href"] = c["href"]
                notices.append(n)
        return page(request, "overview.html", tab="overview", sources=cards, runs=runs_with_source(limit=6),
                    types=connectors(), notices=notices)

    @r.get("/status-pill", response_class=HTMLResponse)
    def status_pill(request: Request):
        return tpl.TemplateResponse(request, "_pill.html", {"stats": lr_stats(fresh=True)})

    # ---- sources --------------------------------------------------------
    @r.get("/sources", response_class=HTMLResponse)
    def sources(request: Request):
        return page(request, "sources.html", tab="sources", sources=all_cards(), types=connectors())

    def parse_config(cls, form) -> dict:
        cfg = {}
        for f in cls.config_schema:
            cfg[f.name] = ("on" if form.get(f.name) else "") if f.type == "bool" else form.get(f.name, "")
        return cfg

    def form_page(request, cls, source, cfg, error=None, status_code=200):
        conn_fields, ingest_rows = v.form_layout(cls.config_schema)
        resp = page(request, "source_form.html", tab="sources", cls=cls, source=source, cfg=cfg, error=error,
                    conn_fields=conn_fields, ingest_rows=ingest_rows,
                    types=connectors(), hide_actions=True, schedule=(source or {}).get("schedule") or cfg.get("_schedule", "manual"),
                    name=(source or {}).get("name") or cfg.get("_name", ""))
        resp.status_code = status_code
        return resp

    @r.get("/sources/new", response_class=HTMLResponse)
    def source_new(request: Request, type: str = "confluence"):
        try:
            cls = get(type)
        except ConnectorError as e:
            raise HTTPException(404, str(e))
        return form_page(request, cls, None, {f.name: f.default for f in cls.config_schema})

    @r.post("/sources/test", response_class=HTMLResponse)
    async def source_test_unsaved(request: Request):
        form = await request.form()
        try:
            cls = get(form.get("type", ""))
            cfg = parse_config(cls, form)
            if form.get("source_id"):  # editing: blank secret = stored one
                old = db.source(int(form["source_id"])) or {"config": {}}
                for f in cls.config_schema:
                    if f.type == "password" and not cfg[f.name]:
                        cfg[f.name] = old["config"].get(f.name, "")
            msg, ok = cls(cfg).test(), True
        except Exception as e:  # noqa: BLE001
            msg, ok = str(e), False
        return tpl.TemplateResponse(request, "_test_result.html", {"ok": ok, "msg": msg})

    @r.post("/sources")
    async def source_create(request: Request):
        form = await request.form()
        cls = get(form["type"])
        cfg = parse_config(cls, form)
        sched_ = form.get("schedule", "manual")
        try:
            if not form.get("name", "").strip():
                raise ConnectorError("'Name' is required")
            cls.validate(cfg)
        except ConnectorError as e:
            return form_page(request, cls, None, {**cfg, "_name": form.get("name", ""), "_schedule": sched_}, str(e), 422)
        sid = db.run("insert into sources(name,type,config,schedule,next_run_at,created_at) values (?,?,?,?,?,?)",
                     form["name"].strip(), cls.type, json.dumps(cfg), sched_, next_run(sched_), now())
        if form.get("run_now"):
            rid = runner.start(sid)
            return go(f"/runs/{rid}", "Source created and first run started")
        return go(f"/sources/{sid}", "Source created")

    @r.get("/sources/{sid}", response_class=HTMLResponse)
    def source_detail(request: Request, sid: int, view: str = "runs"):
        src = db.source(sid) or _404()
        if src["type"] == "files":
            return go("/files")
        card = source_card(src)
        runs = [v.run_view(x) for x in db.all("select * from runs where source_id=? order by id desc limit 50", sid)]
        docs = db.all("select * from documents where source_id=? order by updated_at desc limit 500", sid)
        cls = get(src["type"])
        cfg_rows = []
        for f in cls.config_schema:
            val = src["config"].get(f.name)
            if f.type == "password":
                shown = "•" * 12 if val else ""
            elif f.type == "bool":
                shown = "on" if val in (True, "on", "true") else "off"
            else:
                shown = val if val not in (None, "") else ""
            cfg_rows.append({"label": f.label, "value": shown, "secret": f.type == "password" and bool(val),
                             "mono": f.type in ("text", "textarea", "password")})
        total = db.one("select count(*) n from runs where source_id=?", sid)["n"]
        return page(request, "source_detail.html", tab="sources", src=card, runs=runs, docs=docs, cls=cls,
                    cfg_rows=cfg_rows, view=view if view in ("runs", "docs", "config") else "runs", total_runs=total,
                    active=any(x["active"] for x in runs))

    @r.get("/sources/{sid}/edit", response_class=HTMLResponse)
    def source_edit(request: Request, sid: int):
        src = db.source(sid) or _404()
        cls = get(src["type"])
        return form_page(request, cls, src, src["config"])

    @r.post("/sources/{sid}/edit")
    async def source_update(request: Request, sid: int):
        src = db.source(sid) or _404()
        form = await request.form()
        cls = get(src["type"])
        cfg = parse_config(cls, form)
        for f in cls.config_schema:  # blank password = keep stored secret
            if f.type == "password" and not cfg[f.name]:
                cfg[f.name] = src["config"].get(f.name, "")
        sched_ = form.get("schedule", "manual")
        try:
            if not form.get("name", "").strip():
                raise ConnectorError("'Name' is required")
            cls.validate(cfg)
        except ConnectorError as e:
            return form_page(request, cls, {**src, "name": form.get("name", ""), "schedule": sched_}, cfg, str(e), 422)
        db.update("sources", sid, name=form["name"].strip(), config=json.dumps(cfg), schedule=sched_,
                  enabled=1 if form.get("enabled") else 0, next_run_at=next_run(sched_))
        if form.get("run_now"):
            try:
                rid = runner.start(sid)
                return go(f"/runs/{rid}", "Settings saved and run started")
            except ValueError:
                pass
        return go(f"/sources/{sid}", "Settings saved")

    @r.post("/sources/{sid}/test", response_class=HTMLResponse)
    def source_test(request: Request, sid: int):
        src = db.source(sid) or _404()
        try:
            msg, ok = get(src["type"])(src["config"]).test(), True
        except Exception as e:  # noqa: BLE001
            msg, ok = str(e), False
        return tpl.TemplateResponse(request, "_test_result.html", {"ok": ok, "msg": msg})

    @r.post("/sources/{sid}/run")
    def source_run(sid: int):
        src = db.source(sid) or _404()
        if src["type"] == "files":
            return go("/files")
        try:
            rid = runner.start(sid)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return go(f"/runs/{rid}")

    def no_active_run(sid: int):
        if db.one("select 1 x from runs where source_id=? and status in (?,?,?)", sid, *ACTIVE):
            raise HTTPException(409, "a run is active for this source — cancel it or wait for it to finish")

    @r.post("/sources/{sid}/purge")
    def source_purge(sid: int, back: str = Form("")):
        src = db.source(sid) or _404()
        no_active_run(sid)
        n = runner.purge_source(sid)
        dest = back if back.startswith("/") else ("/files" if src["type"] == "files" else f"/sources/{sid}")
        return go(dest, f"Deleted {n} document(s) of {src['name']} from LightRAG")

    @r.post("/sources/{sid}/delete")
    def source_delete(sid: int, purge: str = Form("")):
        src = db.source(sid) or _404()
        if src["type"] == "files":
            raise HTTPException(400, "built-in source")
        no_active_run(sid)
        if purge:
            runner.purge_source(sid)
        db.run("delete from documents where source_id=?", sid)
        db.run("delete from run_log where run_id in (select id from runs where source_id=?)", sid)
        db.run("delete from runs where source_id=?", sid)
        db.run("delete from sources where id=?", sid)
        return go("/sources", f"Removed source {src['name']}")

    # ---- runs -----------------------------------------------------------
    @r.get("/runs", response_class=HTMLResponse)
    def runs(request: Request, source: int | None = None, status: str = ""):
        where, args = [], []
        if source:
            where.append("r.source_id=?")
            args.append(source)
        kinds = {"completed": ("completed",), "errors": ("completed_with_errors",), "failed": ("failed", "interrupted"),
                 "cancelled": ("cancelled",), "running": ACTIVE}
        if status in kinds:
            where.append("r.status in (%s)" % ",".join("?" * len(kinds[status])))
            args += list(kinds[status])
        rows = runs_with_source(("where " + " and ".join(where)) if where else "", *args, limit=200)
        nav_src = [{"id": None, "label": "All runs", "count": db.one("select count(*) n from runs")["n"]}]
        for x in db.all("select s.id, s.name, count(r.id) n from sources s left join runs r on r.source_id=s.id "
                        "group by s.id order by s.type='files', s.id"):
            nav_src.append({"id": x["id"], "label": x["name"], "count": x["n"]})
        cur = db.source(source) if source else None
        runnable = [c for c in all_cards() if not c["is_files"]]
        return page(request, "runs.html", tab="runs", runs=rows, nav_src=nav_src, source=source, status=status,
                    heading=cur["name"] if cur else "All runs", runnable=runnable)

    def run_ctx(rid: int) -> dict:
        row = db.one("select r.*, s.name sname, s.type stype from runs r join sources s on s.id=r.source_id where r.id=?",
                     rid) or _404()
        run = v.run_view(row)
        log = db.all("select ts,msg from run_log where run_id=? order by rowid", rid)
        for i, l in enumerate(log, 1):
            l["no"], l["err"] = i, l["msg"].startswith(("FAILED", "ERROR", "LightRAG failed", "skip page", "track error"))
        docs = db.all("select * from documents where run_id=? order by id", rid)
        replaced = row["deleted"] or 0
        errors = v.group_errors(log)
        return dict(run=run, log=log, docs=docs, errors=errors, active=run["active"],
                    new=max((row["submitted"] or 0) - replaced, 0), replaced=replaced,
                    failed_docs=row["failed"] or 0,
                    finished=(row["processed"] or 0) + (row["failed"] or 0),
                    unit={"confluence": "Confluence pages", "jira": "Jira issues", "files": "files"}.get(row["stype"], "documents"))

    @r.get("/runs/{rid}", response_class=HTMLResponse)
    def run_page(request: Request, rid: int):
        return page(request, "run_detail.html", tab="runs", **run_ctx(rid))

    @r.get("/runs/{rid}/panel", response_class=HTMLResponse)
    def run_panel(request: Request, rid: int):
        return tpl.TemplateResponse(request, "_run_body.html", run_ctx(rid))

    @r.post("/runs/{rid}/cancel")
    def run_cancel(rid: int):
        runner.request_cancel(rid)
        return go(f"/runs/{rid}", "Cancellation requested")

    @r.post("/runs/{rid}/rerun")
    def run_rerun(rid: int):
        row = db.one("select source_id from runs where id=?", rid) or _404()
        return source_run(row["source_id"])

    @r.get("/api/runs/{rid}")
    def run_json(rid: int):
        return JSONResponse(db.one("select * from runs where id=?", rid) or _404())

    # ---- direct documents -------------------------------------------------
    def staged() -> list[dict]:
        return [{"name": p.name, "size": p.stat().st_size} for p in sorted(staged_dir.iterdir()) if p.is_file()]

    @r.get("/files", response_class=HTMLResponse)
    def files_page(request: Request):
        src = files_source()
        docs = db.all("select * from documents where source_id=? order by updated_at desc", src["id"])
        return page(request, "upload.html", tab="sources", hide_actions=True, src=src, docs=docs, staged=staged(),
                    existing={d["title"]: v.fmt_short(d["updated_at"]) for d in docs})

    def ingest_files(paths: list[tuple[str, Path]]):
        src = files_source()
        try:
            rid = runner.start(src["id"], files=paths)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return go(f"/runs/{rid}")

    @r.post("/files")
    async def files_upload(request: Request, files: list[UploadFile] = File(...), ingest: str = Form("1")):
        target = Path(s.data_dir, "uploads", now().replace(":", "")) if ingest == "1" else staged_dir
        target.mkdir(parents=True, exist_ok=True)
        saved = []
        for f in files:
            if not f.filename:
                continue
            name = Path(f.filename).name
            dest = target / name
            with dest.open("wb") as out:
                shutil.copyfileobj(f.file, out)
            saved.append((name, dest))
        if not saved:
            return go("/files", "No files selected", "warn")
        if ingest != "1":
            return go("/files", f"Uploaded {len(saved)} file(s). They are staged and not ingested yet.")
        return ingest_files(saved)

    @r.post("/files/staged/ingest")
    def staged_ingest():
        items = staged()
        if not items:
            return go("/files", "Nothing staged", "warn")
        folder = Path(s.data_dir, "uploads", now().replace(":", ""))
        folder.mkdir(parents=True, exist_ok=True)
        moved = []
        for it in items:
            dest = folder / it["name"]
            shutil.move(str(staged_dir / it["name"]), dest)
            moved.append((it["name"], dest))
        return ingest_files(moved)

    @r.post("/files/staged/remove")
    def staged_remove(name: str = Form(...)):
        p = staged_dir / Path(name).name
        if p.is_file():
            p.unlink()
        return go("/files", f"Removed {p.name} from the staging area")

    # ---- documents / data management -------------------------------------
    @r.get("/documents", response_class=HTMLResponse)
    def documents(request: Request, q: str = "", source: int | None = None, status: str = ""):
        where, args = ["(d.file_source like ? or d.title like ?)"], [f"%{q}%", f"%{q}%"]
        if source:
            where.append("d.source_id=?")
            args.append(source)
        base_where = " and ".join(where)
        counts = {x["status"]: x["n"] for x in db.all(
            f"select d.status, count(*) n from documents d where {base_where} group by d.status", *args)}
        groups = {"processed": ("processed",), "pending": ("submitted", "processing"), "failed": ("failed",)}
        if status in groups:
            where.append("d.status in (%s)" % ",".join("?" * len(groups[status])))
            args += list(groups[status])
        rows = db.all("select d.*, s.name sname, s.type stype from documents d join sources s on s.id=d.source_id "
                      f"where {' and '.join(where)} order by d.updated_at desc limit 1000", *args)
        tabs = {"all": sum(counts.values()), "processed": counts.get("processed", 0),
                "pending": counts.get("submitted", 0) + counts.get("processing", 0), "failed": counts.get("failed", 0)}
        return page(request, "documents.html", tab="documents", docs=rows, q=q, source=source, status=status or "all",
                    tabs=tabs, sources=db.all("select id,name from sources order by type='files', id"))

    TEXT_EXT = {".md", ".markdown", ".txt", ".csv", ".json", ".log", ".yaml", ".yml", ".xml", ".html", ".htm", ".rst"}

    @r.get("/documents/{pk}", response_class=HTMLResponse)
    def document_view(request: Request, pk: int):
        d = db.one("select d.*, s.name sname, s.type stype from documents d join sources s on s.id=d.source_id "
                   "where d.id=?", pk) or _404()
        f = runner.stored_file(pk)
        body, kind = None, "missing"
        if f:
            ext = f.suffix.lower()
            if ext in TEXT_EXT and f.stat().st_size <= 5 * 1024 * 1024:
                text = f.read_text("utf-8", errors="replace")
                body, kind = (_md(html.escape(text)), "markdown") if ext in (".md", ".markdown") else (text, "text")
            else:
                kind = "binary"
        run = db.one("select * from runs where id=?", d["run_id"]) if d["run_id"] else None
        return page(request, "document.html", tab="documents", d=d, body=body, kind=kind, file=f,
                    size=f.stat().st_size if f else d["size"], run=v.run_view(run) if run else None,
                    st=v.doc_status(d["status"]))

    @r.get("/documents/{pk}/raw")
    def document_raw(pk: int, download: int = 0):
        from fastapi.responses import FileResponse
        f = runner.stored_file(pk) or _404()
        text = f.suffix.lower() in TEXT_EXT
        return FileResponse(f, filename=f.name, media_type="text/plain; charset=utf-8" if text and not download else None,
                            content_disposition_type="attachment" if download or not text else "inline")

    @r.post("/documents/{pk}/delete")
    def document_delete(pk: int, back: str = Form("/documents")):
        runner.delete_docs([pk])
        return go(back if back.startswith("/") else "/documents", "Document deleted from LightRAG")

    @r.post("/documents/bulk")
    async def documents_bulk(request: Request):
        form = await request.form()
        ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
        back = form.get("back", "/documents")
        back = back if back.startswith("/") else "/documents"
        if not ids:
            return go(back, "Nothing selected", "warn")
        if form.get("action") == "reingest":
            rows = [db.one("select * from documents where id=?", i) for i in ids]
            started, skipped = set(), 0
            for row in filter(None, rows):
                src = db.source(row["source_id"])
                if src["type"] == "files":
                    skipped += 1
                    continue
                db.update("documents", row["id"], content_hash="")  # forces re-send on the next run
                started.add(src["id"])
            for sid in started:
                try:
                    runner.start(sid)
                except ValueError:
                    pass
            msg = f"Re-ingesting {len(ids) - skipped} document(s) in {len(started)} run(s)"
            if skipped:
                msg += f"; {skipped} uploaded file(s) skipped (upload them again to re-ingest)"
            return go("/runs" if started else back, msg, "ok" if started else "warn")
        n = runner.delete_docs(ids)
        return go(back, f"Deleted {n} document(s) from LightRAG")

    @r.post("/admin/clear")
    def clear_all(confirm: str = Form("")):
        if confirm.strip() != s.project:
            raise HTTPException(400, f"type the project name '{s.project}' to confirm")
        runner.clear_all()
        return go("/settings", "All data cleared from LightRAG")

    @r.post("/admin/cancel-pipeline")
    def cancel_pipeline(back: str = Form("/")):
        lr = runner.lr()
        try:
            res = lr._req("POST", "/documents/cancel_pipeline")
        finally:
            lr.close()
        msg = "Cancellation requested" if res.get("status") == "cancellation_requested" else "LightRAG is not processing"
        return go(back if back.startswith("/") else "/", msg)

    # ---- settings -------------------------------------------------------
    @r.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        rows = db.all("select s.id, s.name, count(d.id) n from sources s left join documents d on d.source_id=s.id "
                      "group by s.id order by s.type='files', s.id")
        return page(request, "settings.html", tab="settings", src_rows=rows, api_key=s.lightrag_api_key)

    # ---- ask --------------------------------------------------------------
    MODES = [("mix", "Knowledge graph and vector retrieval together. A good default for most questions."),
             ("hybrid", "Combines local and global graph retrieval."),
             ("local", "Entity-centred. Best for specific facts about a named thing."),
             ("global", "Relationship and theme level. Best for broad, cross-document questions."),
             ("naive", "Plain vector search over chunks, no graph. Fastest; useful as a baseline.")]

    def ask_ctx(q="", mode="mix", answer=None, refs=None, error=None):
        mode = mode if mode in dict(MODES) else "mix"
        n_src = db.one("select count(distinct source_id) n from documents")["n"]
        return dict(q=q, mode=mode, modes=MODES, mode_desc=dict(MODES)[mode], answer=answer, refs=refs or [],
                    error=error, n_src=n_src)

    @r.get("/ask", response_class=HTMLResponse)
    def ask_page(request: Request, mode: str = "mix"):
        return page(request, "ask.html", tab="ask", **ask_ctx(mode=mode))

    @r.post("/ask", response_class=HTMLResponse)
    def ask(request: Request, q: str = Form(...), mode: str = Form("mix")):
        lr = runner.lr()
        answer, refs, error = None, [], None
        try:
            res = lr._req("POST", "/query", json={"query": q, "mode": mode, "include_references": True})
            answer = _md(html.escape(res.get("response", "")))
            for ref in res.get("references") or []:
                fp = ref.get("file_path", "")
                d = db.one("select d.id, d.title, s.name sname from documents d join sources s on s.id=d.source_id "
                           "where d.file_source=? or d.file_source like ?", fp, f"%/{fp}")
                refs.append({"id": ref.get("reference_id", ""), "file": fp, "title": d["title"] if d else fp,
                             "pk": d["id"] if d else None,
                             "src": d["sname"] if d else "LightRAG"})
        except LightRAGError as e:
            error = str(e)
        finally:
            lr.close()
        return page(request, "ask.html", tab="ask", **ask_ctx(q, mode, answer, refs, error))

    # ---- guides -----------------------------------------------------------
    def guide_html(name: str) -> str:
        if name in GUIDE_FILES:
            return _md((HERE / "guides" / GUIDE_FILES[name]).read_text().replace("{{base}}", s.base_path))
        return _md(get(name).guide_text() or "_No guide provided._")

    # ---- developers: package + samples for direct API integration ---------
    SAMPLES = {"confluence": ("confluence_ingest.py", "Confluence (wiki)"), "jira": ("jira_ingest.py", "Jira")}

    def portal_url(request: Request) -> str:
        if s.public_api_url.rstrip("/").endswith("/api"):
            return s.public_api_url.rstrip("/")[:-4] + "/mgmt"
        return str(request.base_url).rstrip("/") + s.base_path

    def sample_text(request: Request, name: str) -> str:
        from importlib import resources
        txt = resources.files("mmrag_connectors").joinpath(f"samples/{name}").read_text("utf-8")
        return (txt.replace("{{LIGHTRAG_URL}}", api_url)
                .replace("{{PACKAGE_URL}}", portal_url(request) + "/downloads/mmrag_connectors.zip"))

    def package_dir() -> Path | None:
        import mmrag_connectors
        cands = [os.environ.get("PORTAL_PKG_SRC"), str(Path(mmrag_connectors.__file__).parents[2]), "/src/mmrag_connectors"]
        for c in filter(None, cands):
            if (Path(c) / "pyproject.toml").is_file():
                return Path(c)
        return None

    @r.get("/downloads/mmrag_connectors.zip")
    def download_package():
        import io
        import zipfile
        from fastapi.responses import Response
        root = package_dir() or _404()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(root.rglob("*")):
                rel = f.relative_to(root)
                if f.is_file() and not any(part in ("__pycache__", ".pytest_cache") or part.endswith(".egg-info")
                                           for part in rel.parts):
                    z.write(f, Path("mmrag_connectors") / rel)
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="mmrag_connectors.zip"'})

    @r.get("/downloads/samples/{name}")
    def download_sample(request: Request, name: str):
        from fastapi.responses import Response
        if name not in {f for f, _ in SAMPLES.values()}:
            raise HTTPException(404)
        return Response(sample_text(request, name), media_type="text/x-python; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @r.get("/guides/api", response_class=HTMLResponse)
    def guide_api(request: Request):
        md = (HERE / "guides" / "api.md").read_text()
        for k, val in {"{{base}}": s.base_path, "{{API}}": api_url, "{{PKG}}": portal_url(request) + "/downloads/mmrag_connectors.zip",
                       "{{PORTAL}}": portal_url(request)}.items():
            md = md.replace(k, val)
        return page(request, "guides.html", tab="guides", types=connectors(), current="api", guide_html=_md(md),
                    downloads=[("Python package (.zip)", f"{s.base_path}/downloads/mmrag_connectors.zip")])

    @r.get("/guides/api-{kind}", response_class=HTMLResponse)
    def guide_api_sample(request: Request, kind: str):
        if kind not in SAMPLES:
            raise HTTPException(404)
        fname, label = SAMPLES[kind]
        md = (HERE / "guides" / f"api-{kind}.md").read_text()
        for k, val in {"{{base}}": s.base_path, "{{API}}": api_url, "{{PKG}}": portal_url(request) + "/downloads/mmrag_connectors.zip",
                       "{{SAMPLE_URL}}": portal_url(request) + f"/downloads/samples/{fname}"}.items():
            md = md.replace(k, val)
        return page(request, "guides.html", tab="guides", types=connectors(), current=f"api-{kind}", guide_html=_md(md),
                    sample=sample_text(request, fname), sample_name=fname,
                    downloads=[(fname, f"{s.base_path}/downloads/samples/{fname}"),
                               ("Python package (.zip)", f"{s.base_path}/downloads/mmrag_connectors.zip")])

    @r.get("/guides", response_class=HTMLResponse)
    def guides(request: Request):
        return page(request, "guides.html", tab="guides", types=connectors(), current="overview",
                    guide_html=guide_html("overview"))

    @r.get("/guides/{name}", response_class=HTMLResponse)
    def guide(request: Request, name: str):
        if name not in GUIDE_FILES:
            get(name)  # 404s via ConnectorError handler below if unknown
        return page(request, "guides.html", tab="guides", types=connectors(), current=name, guide_html=guide_html(name))

    app.include_router(r, prefix=s.base_path)
    app.mount(f"{s.base_path}/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.exception_handler(LightRAGError)
    async def lightrag_error(request: Request, exc: LightRAGError):
        busy = isinstance(exc, LightRAGBusy)
        resp = page(request, "error.html", title="LightRAG is busy" if busy else "LightRAG error", message=str(exc),
                    hint="LightRAG refuses deletions while it is processing documents. Try again once the pipeline is idle."
                    if busy else "Check that the LightRAG service is running.")
        resp.status_code = 409 if busy else 502
        return resp

    @app.exception_handler(ConnectorError)
    async def connector_error(request: Request, exc: ConnectorError):
        resp = page(request, "error.html", title="Not found", message=str(exc), hint="")
        resp.status_code = 404
        return resp

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.headers.get("hx-request") or not request.url.path.startswith(s.base_path or "/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        resp = page(request, "error.html", title=f"Error {exc.status_code}", message=str(exc.detail), hint="")
        resp.status_code = exc.status_code
        return resp

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


def _404():
    raise HTTPException(404)


def _md(text: str) -> str:
    return markdown.markdown(text, extensions=["tables", "fenced_code"])


app = create_app() if os.environ.get("PORTAL_AUTOSTART", "1") == "1" else None
