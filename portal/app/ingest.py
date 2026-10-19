"""Ingestion pipeline: connector -> dedupe -> LightRAG API -> monitor.  (user -> mgmt -> config -> trigger -> ingest -> lightrag -> monitor)"""
from __future__ import annotations

import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from mmrag_connectors import ConnectorError, LightRAGBusy, LightRAGClient, LightRAGError, get

from .config import Settings
from .db import ACTIVE, DB, now

INTERVALS = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}
TERMINAL = {"PROCESSED": "processed", "FAILED": "failed"}


def next_run(schedule: str) -> str | None:
    d = INTERVALS.get(schedule)
    return (datetime.now(timezone.utc) + d).strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


class Cancelled(Exception):
    pass


class Untracked(Exception):
    def __init__(self, doc_id: str):
        self.doc_id = doc_id


@dataclass
class Item:
    ext_id: str
    title: str
    url: str
    file_source: str
    chash: str
    size: int
    text: str | None = None
    file: Path | None = None
    spool: Path | None = None

    def spooled(self, folder: Path) -> "Item":
        """Park text content on disk while it waits for its old version to be deleted."""
        if self.text is not None:
            folder.mkdir(parents=True, exist_ok=True)
            self.spool = folder / f"{abs(hash(self.ext_id))}.md"
            self.spool.write_text(self.text, "utf-8")
            self.text = None
        return self

    def load(self) -> str:
        return self.text if self.text is not None else self.spool.read_text("utf-8")


class Runner:
    def __init__(self, db: DB, s: Settings):
        self.db, self.s = db, s
        self.pool = ThreadPoolExecutor(max_workers=s.max_parallel_runs)
        self.cancel: dict[int, threading.Event] = {}
        for r in db.all("select id from runs where status in (?,?,?)", *ACTIVE):
            db.update("runs", r["id"], status="interrupted", finished_at=now(), error="portal restarted during run")

    def lr(self) -> LightRAGClient:
        return LightRAGClient(self.s.lightrag_url, self.s.lightrag_api_key)

    # ---- entry points ---------------------------------------------------
    def start(self, source_id: int, trigger: str = "manual", files: list[tuple[str, Path]] | None = None) -> int:
        if self.db.one("select 1 x from runs where source_id=? and status in (?,?,?)", source_id, *ACTIVE):
            raise ValueError("a run is already active for this source")
        rid = self.db.run("insert into runs(source_id,trigger,status,started_at) values (?,?,?,?)",
                          source_id, trigger, "pending", now())
        self.cancel[rid] = threading.Event()
        self.pool.submit(self._guard, rid, files)
        return rid

    def shutdown(self) -> None:
        for ev in list(self.cancel.values()):
            ev.set()
        self.pool.shutdown(wait=True, cancel_futures=True)

    def request_cancel(self, run_id: int) -> None:
        if ev := self.cancel.get(run_id):
            ev.set()

    # ---- run ------------------------------------------------------------
    def _guard(self, rid: int, files) -> None:
        src_id = self.db.one("select source_id from runs where id=?", rid)["source_id"]
        src = self.db.source(src_id)
        status, err = "completed", None
        try:
            self._execute(rid, src, files)
        except Cancelled:
            status = "cancelled"
        except (ConnectorError, LightRAGError) as e:
            status, err = "failed", str(e)
        except Exception as e:  # noqa: BLE001 - surface anything to the UI
            status, err = "failed", f"{type(e).__name__}: {e}"
        r = self.db.one("select failed from runs where id=?", rid)
        if status == "completed" and r["failed"]:
            status = "completed_with_errors"
        if err:
            self.db.log(rid, f"ERROR {err}")
        self.db.update("runs", rid, status=status, finished_at=now(), error=err)
        upd = {"last_run_at": now(), "next_run_at": next_run(src["schedule"])}
        if status.startswith("completed"):
            upd["last_success_at"] = self.db.one("select started_at from runs where id=?", rid)["started_at"]
        self.db.update("sources", src_id, **upd)
        self.cancel.pop(rid, None)

    def _check(self, rid: int) -> None:
        if self.cancel[rid].is_set():
            raise Cancelled()

    def _execute(self, rid: int, src: dict, files) -> None:
        """new docs: insert now. changed docs: spool, and once the new ones are processed (LightRAG refuses
        deletes while busy) delete the old versions and insert the new ones."""
        db, lr = self.db, self.lr()
        spool = Path(self.s.data_dir, "spool", str(rid))
        try:
            lr.health()
            db.update("runs", rid, status="fetching")
            sent: list[int] = []
            changed: list[tuple[Item, dict, str]] = []
            for item in self._items(rid, src, files, spool):
                self._check(rid)
                db.bump(rid, discovered=1)
                row = db.one("select * from documents where source_id=? and external_id=?", src["id"], item.ext_id)
                if row and row["content_hash"] == item.chash and row["status"] in ("processed", "submitted", "processing"):
                    db.bump(rid, unchanged=1)
                    continue
                old = self._doc_id_for(lr, row) if row else None
                if old:
                    changed.append((item.spooled(spool), row, old))
                    continue
                try:
                    if pk := self._insert(rid, src, lr, item, row):
                        sent.append(pk)
                except Untracked as u:
                    owner = self._owner_of(u.doc_id, item.file_source, src["id"])
                    if owner:  # same item reached through another source (e.g. a Jira ticket linked from Confluence)
                        db.bump(rid, unchanged=1)
                        db.log(rid, f"{item.file_source} is already ingested by source '{owner}'; skipped")
                        continue
                    # LightRAG holds it but no source tracks it (e.g. portal DB lost) -> take it over
                    db.log(rid, f"{item.file_source} already exists in LightRAG; it will be replaced")
                    changed.append((item.spooled(spool), row, u.doc_id))
            db.update("runs", rid, status="monitoring")
            db.log(rid, f"submitted {len(sent)} new document(s); {len(changed)} changed document(s) to replace")
            self._monitor(rid, lr, sent)
            if changed:
                self._replace(rid, src, lr, changed)
        finally:
            lr.close()
            shutil.rmtree(spool, ignore_errors=True)

    def _items(self, rid, src, files, spool) -> Iterator["Item"]:
        if src["type"] == "files":
            for name, path in files or []:
                yield Item(f"file:{name}", name, "", name, _filehash(path), path.stat().st_size, file=path)
            return
        conn = get(src["type"])(src["config"], log=lambda m: self.db.log(rid, m))
        since = None
        if self.db.one("select trigger from runs where id=?", rid)["trigger"] == "schedule" and src["last_success_at"]:
            since = datetime.strptime(src["last_success_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        for doc in conn.fetch(since):
            yield Item(doc.external_id, doc.title, doc.url, doc.file_source, doc.content_hash, len(doc.content), text=doc.content)

    def _replace(self, rid, src, lr, changed) -> None:
        db = self.db
        olds = [old for _, _, old in changed]
        db.update("runs", rid, status="monitoring")
        db.log(rid, f"deleting {len(olds)} outdated document(s) (waits until the LightRAG pipeline is idle)")
        deadline = time.time() + self.s.monitor_timeout
        while True:
            self._check(rid)
            try:
                lr.delete_documents(olds)
                break
            except LightRAGBusy:
                if time.time() > deadline:
                    for item, row, _ in changed:
                        self._record(rid, src, item, row, None, "LightRAG stayed busy; old version could not be replaced")
                    return
                time.sleep(max(self.s.poll_interval, 5))
        db.bump(rid, deleted=len(olds))
        sent = [pk for item, row, _ in changed if (pk := self._insert(rid, src, lr, item, row, wait_conflict=True))]
        db.log(rid, f"re-submitted {len(sent)} changed document(s)")
        self._monitor(rid, lr, sent)

    def _owner_of(self, doc_id: str, file_source: str, source_id: int) -> str | None:
        """Name of another source that already tracks this LightRAG document, if any."""
        base = file_source.rsplit("/", 1)[-1]
        r = self.db.one("select s.name from documents d join sources s on s.id=d.source_id where d.source_id!=? and "
                        "(d.lightrag_doc_id=? or d.file_source=? or d.file_source like ?)", source_id, doc_id, base, f"%/{base}")
        return r["name"] if r else None

    def _doc_id_for(self, lr: LightRAGClient, row: dict) -> str | None:
        if row.get("lightrag_doc_id"):
            return row["lightrag_doc_id"]
        if row.get("track_id"):
            try:
                docs = lr.track(row["track_id"]).get("documents", [])
                return docs[0]["id"] if docs else None
            except LightRAGError:
                return None
        return None

    def _insert(self, rid, src, lr, item: "Item", row, wait_conflict=False) -> int | None:
        deadline = time.time() + (self.s.conflict_wait if wait_conflict else 0)
        track, err = None, None
        while True:
            try:
                track = lr.upload_file(item.file, item.file_source) if item.file else lr.insert_text(item.load(), item.file_source)
                break
            except LightRAGError as e:
                if e.status == 409 and time.time() < deadline:
                    self._check(rid)
                    time.sleep(3)  # previous version still being deleted in the background
                    continue
                if e.status == 409 and not wait_conflict:
                    existing = lr.find_by_file_path(item.file_source)
                    if existing:
                        raise Untracked(existing["id"])
                err = str(e)
                break
        return self._record(rid, src, item, row, track, err)

    def _record(self, rid, src, item, row, track, err) -> int | None:
        db = self.db
        vals = dict(run_id=rid, title=item.title, url=item.url, file_source=item.file_source, content_hash=item.chash,
                    track_id=track, lightrag_doc_id=None, status="submitted" if track else "failed", error=err,
                    size=item.size, updated_at=now())
        if row and db.one("select 1 x from documents where id=?", row["id"]):
            db.update("documents", row["id"], **vals)
            pk = row["id"]
        else:
            pk = db.run("insert into documents(source_id,external_id) values (?,?)", src["id"], item.ext_id)
            db.update("documents", pk, **vals)
        if track:
            self._store(pk, item)
            db.bump(rid, submitted=1)
            return pk
        db.bump(rid, failed=1)
        db.log(rid, f"FAILED to submit {item.file_source}: {err}")
        return None

    def _monitor(self, rid: int, lr: LightRAGClient, pks: list[int]) -> None:
        db = self.db
        pending = set(pks)
        deadline = time.time() + self.s.monitor_timeout
        while pending:
            self._check(rid)
            if time.time() > deadline:
                db.log(rid, f"monitor timeout; {len(pending)} document(s) still processing in LightRAG")
                return
            for pk in list(pending):
                row = db.one("select * from documents where id=?", pk)
                if not row:  # deleted from the UI meanwhile
                    pending.discard(pk)
                    continue
                try:
                    docs = lr.track(row["track_id"]).get("documents", [])
                except LightRAGError as e:
                    db.log(rid, f"track error: {e}")
                    continue
                if not docs:
                    continue
                d = docs[0]
                st = str(d.get("status", "")).upper()
                if st in TERMINAL:
                    db.update("documents", pk, status=TERMINAL[st], lightrag_doc_id=d.get("id"),
                              error=d.get("error_msg") or None, updated_at=now())
                    db.bump(rid, **{TERMINAL[st]: 1})
                    pending.discard(pk)
                    if st == "FAILED":
                        db.log(rid, f"LightRAG failed {row['file_source']}: {d.get('error_msg')}")
                else:
                    db.update("documents", pk, status="processing", lightrag_doc_id=d.get("id"))
            if pending:
                time.sleep(self.s.poll_interval)

    # ---- stored copies (LightRAG has no API to read a document back) -----
    def doc_dir(self, pk: int) -> Path:
        return Path(self.s.data_dir, "docs", str(pk))

    def stored_file(self, pk: int) -> Path | None:
        d = self.doc_dir(pk)
        files = [f for f in d.iterdir() if f.is_file()] if d.is_dir() else []
        return files[0] if files else None

    def _store(self, pk: int, item: "Item") -> None:
        """Keep exactly what was sent to LightRAG so the portal can show it."""
        d = self.doc_dir(pk)
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        name = Path(item.file_source).name or "document.md"
        try:
            if item.file:
                shutil.copyfile(item.file, d / name)
            else:
                (d / name).write_text(item.load(), "utf-8")
        except OSError:
            pass  # viewing is a convenience; never fail ingestion over it

    # ---- data removal ---------------------------------------------------
    def delete_docs(self, doc_pks: list[int]) -> int:
        lr, n = self.lr(), 0
        try:
            rows = [self.db.one("select * from documents where id=?", pk) for pk in doc_pks]
            ids = [i for i in (self._doc_id_for(lr, r) for r in rows if r) if i]
            if ids:
                lr.delete_documents(ids)
            for r in rows:
                if r:
                    self.db.run("delete from documents where id=?", r["id"])
                    shutil.rmtree(self.doc_dir(r["id"]), ignore_errors=True)
                    n += 1
        finally:
            lr.close()
        return n

    def purge_source(self, source_id: int) -> int:
        pks = [d["id"] for d in self.db.all("select id from documents where source_id=?", source_id)]
        n = self.delete_docs(pks)
        self.db.update("sources", source_id, last_success_at=None)  # next run is a full run
        return n

    def clear_all(self) -> None:
        lr = self.lr()
        try:
            lr.clear()
        finally:
            lr.close()
        self.db.run("delete from documents")
        self.db.run("update sources set last_success_at=null")
        shutil.rmtree(Path(self.s.data_dir, "docs"), ignore_errors=True)


def _filehash(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Scheduler(threading.Thread):
    def __init__(self, db: DB, runner: Runner, tick: float):
        super().__init__(daemon=True)
        self.db, self.runner, self.tick = db, runner, tick
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.wait(self.tick):
            try:
                for s in self.db.all("select id from sources where enabled=1 and schedule!='manual' and next_run_at<=?", now()):
                    try:
                        self.runner.start(s["id"], "schedule")
                    except ValueError:
                        pass
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._halt.set()
