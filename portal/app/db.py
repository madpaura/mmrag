import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
create table if not exists sources(
  id integer primary key autoincrement, name text not null, type text not null,
  config text not null default '{}', schedule text not null default 'manual',
  enabled integer not null default 1, next_run_at text, last_run_at text, last_success_at text, created_at text);
create table if not exists runs(
  id integer primary key autoincrement, source_id integer not null, trigger text, status text,
  started_at text, finished_at text, error text,
  discovered integer default 0, unchanged integer default 0, submitted integer default 0, deleted integer default 0,
  processed integer default 0, failed integer default 0);
create table if not exists run_log(run_id integer, ts text, msg text);
create index if not exists run_log_idx on run_log(run_id);
create table if not exists documents(
  id integer primary key autoincrement, source_id integer not null, run_id integer, external_id text not null,
  title text, url text, file_source text, content_hash text, track_id text, lightrag_doc_id text,
  status text, error text, size integer default 0, updated_at text,
  unique(source_id, external_id));
"""

ACTIVE = ("pending", "fetching", "monitoring")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DB:
    def __init__(self, path: str):
        self.c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.c.execute("pragma journal_mode=wal")
            self.c.executescript(SCHEMA)

    def all(self, sql: str, *a) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.c.execute(sql, a).fetchall()]

    def one(self, sql: str, *a) -> dict | None:
        with self.lock:
            r = self.c.execute(sql, a).fetchone()
            return dict(r) if r else None

    def run(self, sql: str, *a) -> int:
        with self.lock:
            return self.c.execute(sql, a).lastrowid

    def update(self, table: str, id_: int, **kw) -> None:
        cols = ", ".join(f"{k}=?" for k in kw)
        self.run(f"update {table} set {cols} where id=?", *kw.values(), id_)

    def bump(self, run_id: int, **kw) -> None:
        cols = ", ".join(f"{k}={k}+?" for k in kw)
        self.run(f"update runs set {cols} where id=?", *kw.values(), run_id)

    def log(self, run_id: int, msg: str) -> None:
        self.run("insert into run_log values (?,?,?)", run_id, now(), msg[:2000])

    # sources ------------------------------------------------------------
    def source(self, id_: int) -> dict | None:
        s = self.one("select * from sources where id=?", id_)
        if s:
            s["config"] = json.loads(s["config"])
        return s
