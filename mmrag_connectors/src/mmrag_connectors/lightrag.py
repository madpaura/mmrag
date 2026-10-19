"""Thin client for the LightRAG server API (v1.5.x).

Base URL includes the API prefix, e.g. ``http://mydomain.com/mmrag/p1/api`` or ``http://rag-app:9621``.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable

import httpx

TERMINAL = {"PROCESSED", "FAILED"}


class LightRAGError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class LightRAGBusy(LightRAGError):
    """LightRAG refused a destructive operation because its pipeline is processing documents."""


class LightRAGClient:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 60, verify: bool = True):
        headers = {"X-API-Key": api_key} if api_key else {}
        self._c = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, verify=verify)

    def close(self) -> None:
        self._c.close()

    def _req(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self._c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise LightRAGError(f"LightRAG unreachable: {e}") from e
        if r.status_code >= 400:
            raise LightRAGError(f"{method} {path} -> {r.status_code}: {r.text[:300]}", r.status_code)
        return r.json() if r.content else {}

    # --- health / stats -------------------------------------------------
    def health(self) -> dict:
        return self._req("GET", "/health")

    def status_counts(self) -> dict[str, int]:
        return self._req("GET", "/documents/status_counts").get("status_counts", {})

    def pipeline_status(self) -> dict:
        return self._req("GET", "/documents/pipeline_status")

    # --- ingest ---------------------------------------------------------
    def insert_text(self, text: str, file_source: str) -> str:
        """Returns track_id. LightRAG keeps only the *basename* of file_source and rejects a basename it already
        holds with LightRAGError(status=409) — make basenames unique per document."""
        return self._req("POST", "/documents/text", json={"text": text, "file_source": file_source})["track_id"]

    def upload_file(self, path: str | Path, name: str | None = None) -> str:
        p = Path(path)
        with p.open("rb") as fh:
            return self._req("POST", "/documents/upload", files={"file": (name or p.name, fh)})["track_id"]

    # --- monitor --------------------------------------------------------
    def track(self, track_id: str) -> dict:
        return self._req("GET", f"/documents/track_status/{track_id}")

    def wait(self, track_ids: Iterable[str], timeout: float = 3600, interval: float = 3, on_update=None) -> dict[str, dict]:
        """Poll until every track_id has only terminal documents (or timeout). Returns {track_id: track_status}."""
        pending = set(track_ids)
        result: dict[str, dict] = {}
        deadline = time.time() + timeout
        while pending and time.time() < deadline:
            for tid in list(pending):
                st = self.track(tid)
                result[tid] = st
                docs = st.get("documents", [])
                if docs and all(str(d.get("status", "")).upper() in TERMINAL for d in docs):
                    pending.discard(tid)
            if on_update:
                on_update(result)
            if pending:
                time.sleep(interval)
        return result

    def find_by_file_path(self, file_path: str) -> dict | None:
        """Scan the document list for a file name (no server-side filter exists). Use sparingly.
        LightRAG stores only the basename of file_source, so that is what is compared."""
        file_path = file_path.rsplit("/", 1)[-1]
        page = 1
        while True:
            d = self._req("POST", "/documents/paginated", json={"page": page, "page_size": 200,
                                                                 "sort_field": "file_path", "sort_direction": "asc"})
            for doc in d.get("documents", []):
                if doc.get("file_path") == file_path:
                    return doc
            pg = d.get("pagination", {})
            if not d.get("documents") or not pg.get("has_next", page < pg.get("total_pages", 0)):
                return None
            page += 1

    # --- delete ---------------------------------------------------------
    def delete_documents(self, doc_ids: list[str], delete_llm_cache: bool = False) -> None:
        """Starts background deletion. Raises LightRAGBusy while the pipeline is processing (LightRAG refuses then)."""
        if doc_ids:
            r = self._req("DELETE", "/documents/delete_document",
                          json={"doc_ids": doc_ids, "delete_file": False, "delete_llm_cache": delete_llm_cache})
            self._raise_if_refused(r)

    def clear(self) -> None:
        self._raise_if_refused(self._req("DELETE", "/documents"))

    @staticmethod
    def _raise_if_refused(r: dict) -> None:
        st = r.get("status")
        if st == "busy":
            raise LightRAGBusy(r.get("message") or "LightRAG pipeline is busy; try again when it is idle")
        if st in ("not_allowed", "fail"):
            raise LightRAGError(r.get("message") or f"LightRAG refused: {st}")

    # --- query ----------------------------------------------------------
    def query(self, q: str, mode: str = "mix") -> str:
        return self._req("POST", "/query", json={"query": q, "mode": mode}).get("response", "")
