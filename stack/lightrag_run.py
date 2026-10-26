"""Entrypoint of the rag-app container (mounted read-only by stack/docker-compose.yml).

* Loads the project's .env on every start, so a restart applies what was saved in the portal's
  LightRAG page (or edited by hand) without recreating the container. Keys set by the compose
  `environment:` block (MMRAG_PINNED_ENV) win over the file, as they would with env_file.
* Restarts on request: the portal writes a new token to <control>/restart; this process stops
  LightRAG and exits, and Docker's restart policy starts the container again.
* Reports what it loaded in <control>/status.json (read by the portal and by setup.sh).

Runs on the LightRAG image's Python; python-dotenv is a LightRAG dependency.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from dotenv import dotenv_values

ENV_FILE = Path(os.environ.get("MMRAG_ENV_FILE", "/mmrag/project/.env"))
CTL = Path(os.environ.get("MMRAG_CONTROL_DIR", "/mmrag/control"))
PINNED = {k.strip() for k in os.environ.get("MMRAG_PINNED_ENV", "").split(",") if k.strip()}
CMD = shlex.split(os.environ.get("MMRAG_LIGHTRAG_CMD", "")) or [sys.executable, "-m", "lightrag.api.lightrag_server"]
STOP_TIMEOUT = float(os.environ.get("MMRAG_STOP_TIMEOUT", "30"))


def log(msg: str) -> None:
    print(f"[mmrag] {msg}", flush=True)


def read_token() -> str:
    try:
        return (CTL / "restart").read_text().strip()
    except OSError:
        return ""


def write_status(**st) -> None:
    CTL.mkdir(parents=True, exist_ok=True)
    tmp = CTL / ".status.json.tmp"
    tmp.write_text(json.dumps(st))
    os.replace(tmp, CTL / "status.json")


def load_env() -> tuple[dict, str]:
    raw = ENV_FILE.read_bytes()
    values = dotenv_values(stream=io.StringIO(raw.decode("utf-8")))
    env = dict(os.environ)
    for k, v in values.items():
        if v is not None and k not in PINNED:
            env[k] = v
    return env, hashlib.sha256(raw).hexdigest()


def stop(child: subprocess.Popen) -> int:
    child.terminate()
    try:
        return child.wait(STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        child.kill()
        return child.wait()


def main() -> int:
    env, sha = load_env()
    token = read_token()  # a request written before this start is already satisfied
    st = {"state": "running", "started_at": time.time(), "env_sha256": sha, "restart_token": token}
    write_status(**st)
    log(f"loaded {ENV_FILE} (sha256 {sha[:12]}), starting LightRAG")
    child = subprocess.Popen(CMD, env=env)
    signal.signal(signal.SIGTERM, lambda *_: child.terminate())  # PID 1: no default handlers
    signal.signal(signal.SIGINT, lambda *_: child.terminate())
    while True:
        try:
            rc = child.wait(timeout=1)
            break
        except subprocess.TimeoutExpired:
            pass
        if read_token() != token:
            log("restart requested by the portal, stopping LightRAG")
            write_status(**{**st, "state": "restarting"})
            stop(child)
            return 0  # restart: unless-stopped brings the container back, which reloads .env
    write_status(**{**st, "state": "exited", "returncode": rc, "exited_at": time.time()})
    return rc


if __name__ == "__main__":
    sys.exit(main())
