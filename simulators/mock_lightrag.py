"""Mock LightRAG server implementing the subset of the v1.5.x API the portal uses.

Documents move PENDING -> PROCESSING -> PROCESSED after MOCK_DELAY seconds (default 2). Text containing
"FAIL_ME" ends FAILED. Same file_source twice -> 409 (like the real server).
Run:  python mock_lightrag.py [--port 9621]
"""
from __future__ import annotations

import argparse
import os
import threading
import time
import uuid

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile

DELAY = float(os.getenv("MOCK_DELAY", "2"))
DOCS: dict[str, dict] = {}
app = FastAPI(title="mock-lightrag")


def status(d) -> str:
    age = time.time() - d["t"]
    if d["fail"] and age > DELAY:
        return "FAILED"
    return "PENDING" if age < DELAY / 3 else "PROCESSING" if age < DELAY else "PROCESSED"


def view(d):
    return {"id": d["id"], "status": status(d), "file_path": d["src"], "track_id": d["track"], "content_length": len(d["text"]),
            "content_summary": d["text"][:80], "created_at": "", "updated_at": "",
            "error_msg": "simulated failure" if d["fail"] and status(d) == "FAILED" else None}


def add(text: str, src: str) -> str:
    src = src.rsplit("/", 1)[-1]  # real LightRAG keeps only the basename and de-duplicates on it
    if any(d["src"] == src for d in DOCS.values()):
        raise HTTPException(409, f"document with file_source '{src}' already exists")
    d = dict(id="doc-" + uuid.uuid4().hex[:12], src=src, text=text, t=time.time(), track="t_" + uuid.uuid4().hex[:10],
             fail="FAIL_ME" in text)
    DOCS[d["id"]] = d
    return d["track"]


@app.get("/health")
def health():
    return {"status": "healthy", "mock": True}


@app.post("/documents/text")
async def text(req: Request):
    b = await req.json()
    return {"status": "success", "message": "queued", "track_id": add(b["text"], b.get("file_source") or "unknown_source")}


@app.post("/documents/upload")
async def upload(file: UploadFile = File(...)):
    return {"status": "success", "message": "queued", "track_id": add((await file.read()).decode("utf-8", "replace"), file.filename)}


@app.get("/documents/track_status/{tid}")
def track(tid: str):
    docs = [view(d) for d in DOCS.values() if d["track"] == tid]
    summ: dict[str, int] = {}
    for d in docs:
        summ[d["status"]] = summ.get(d["status"], 0) + 1
    return {"track_id": tid, "documents": docs, "total_count": len(docs), "status_summary": summ}


@app.post("/documents/paginated")
async def paginated(req: Request):
    b = await req.json()
    size, page = b.get("page_size", 50), b.get("page", 1)
    docs = sorted((view(d) for d in DOCS.values()), key=lambda d: d["file_path"])
    pages = max(1, -(-len(docs) // size))
    return {"documents": docs[(page - 1) * size: page * size],
            "pagination": {"page": page, "page_size": size, "total_count": len(docs), "total_pages": pages,
                           "has_next": page < pages, "has_prev": page > 1}, "status_counts": {}}


@app.get("/documents/status_counts")
def counts():
    c: dict[str, int] = {}
    for d in DOCS.values():
        c[status(d)] = c.get(status(d), 0) + 1
    return {"status_counts": c}


@app.get("/documents/pipeline_status")
def pipeline():
    return {"busy": _busy(), "job_name": "mock", "docs": len(DOCS)}


def _busy() -> bool:
    return any(status(d) in ("PENDING", "PROCESSING") for d in DOCS.values())


def _purge_later(ids):
    time.sleep(DELAY)  # like the real server, deletion completes in the background
    for i in ids:
        DOCS.pop(i, None)


@app.delete("/documents/delete_document")
async def delete(req: Request):
    # real LightRAG answers HTTP 200 {"status":"busy"} and deletes nothing while the pipeline is processing
    if _busy():
        return {"status": "busy", "message": "Pipeline is busy with another operation.", "doc_id": ""}
    threading.Thread(target=_purge_later, args=((await req.json())["doc_ids"],), daemon=True).start()
    return {"status": "deletion_started", "message": "ok", "doc_id": ""}


@app.delete("/documents")
def clear():
    if _busy():
        return {"status": "busy", "message": "Pipeline is busy with another operation."}
    DOCS.clear()
    return {"status": "success", "message": "cleared"}


@app.post("/documents/cancel_pipeline")
def cancel():
    return {"status": "cancellation_requested", "message": "ok"}


@app.post("/query")
async def query(req: Request):
    b = await req.json()
    refs = [{"reference_id": str(i + 1), "file_path": d["src"]} for i, d in enumerate(list(DOCS.values())[:2])]
    out = {"response": f"**[mock]** {len(DOCS)} documents indexed; query was: {b.get('query')} [1]"}
    if b.get("include_references"):
        out["references"] = refs
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9621)
    a = ap.parse_args()
    uvicorn.run(app, host="0.0.0.0", port=a.port, log_level="warning")
