"""Mock OpenAI-compatible LLM + embedding endpoint (stands in for LiteLLM in development).

Lets a real LightRAG stack ingest, delete and query without any model: embeddings are deterministic
hash vectors, chat completions return an empty-but-valid extraction result.
Run:  python mock_llm.py [--port 4001] [--dim 1024]
Point LightRAG at it: LLM_BINDING_HOST / EMBEDDING_BINDING_HOST = http://host.docker.internal:4001/v1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time

import uvicorn
from fastapi import FastAPI, Request

DIM = 1024
app = FastAPI(title="mock-llm")


def vec(text: str) -> list[float]:
    out, seed = [], text.encode()
    while len(out) < DIM:
        seed = hashlib.sha256(seed).digest()
        out += [(b - 127.5) / 127.5 for b in seed]
    out = out[:DIM]
    n = math.sqrt(sum(x * x for x in out)) or 1
    return [x / n for x in out]


@app.get("/v1/models")
@app.get("/health/liveliness")
def models():
    return {"data": [{"id": "mock", "object": "model"}]}


@app.post("/v1/embeddings")
async def embeddings(req: Request):
    b = await req.json()
    inp = b["input"] if isinstance(b["input"], list) else [b["input"]]
    return {"object": "list", "model": b.get("model", "mock"),
            "data": [{"object": "embedding", "index": i, "embedding": vec(str(t))} for i, t in enumerate(inp)],
            "usage": {"prompt_tokens": 0, "total_tokens": 0}}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    b = await req.json()
    prompt = json.dumps(b.get("messages", []))
    if "keywords" in prompt.lower():
        content = json.dumps({"high_level_keywords": ["simulated"], "low_level_keywords": ["simulated"]})
    elif "entit" in prompt.lower():
        content = json.dumps({"entities": [], "relationships": []})
    else:
        content = "[mock-llm] This is a simulated answer."
    return {"id": "mock", "object": "chat.completion", "created": int(time.time()), "model": b.get("model", "mock"),
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4001)
    ap.add_argument("--dim", type=int, default=1024)
    a = ap.parse_args()
    DIM = a.dim
    uvicorn.run(app, host="0.0.0.0", port=a.port, log_level="warning")
