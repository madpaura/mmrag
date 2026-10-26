import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

os.environ["PORTAL_AUTOSTART"] = "0"
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "simulators"))
sys.path.insert(0, str(ROOT / "portal"))


def _serve(app):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return server, port


DEPLOY_BLOCK = """
# ── mmrag deployment (setup.sh) ──
PROJECT=t1
API_PREFIX=/mmrag/t1/api
LIGHTRAG_PORT=19606
LIGHTRAG_API_KEY=mmr_secretkey
MMRAG_SESSION=mmr_sessionsecret
WHITELIST_PATHS=/health,/mmrag/t1/api/health
"""


@pytest.fixture(scope="session")
def atlassian():
    import mock_atlassian
    server, port = _serve(mock_atlassian.app)
    mock_atlassian.BASE = f"http://127.0.0.1:{port}"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture()
def stack(tmp_path, atlassian):
    """Portal (TestClient) wired to a mock LightRAG on a real port."""
    import mock_lightrag
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    mock_lightrag.DOCS.clear()
    mock_lightrag.DELAY = 0.3
    server, port = _serve(mock_lightrag.app)
    s = Settings()
    s.data_dir, s.lightrag_url, s.base_path, s.project = str(tmp_path), f"http://127.0.0.1:{port}", "/mmrag/t1/mgmt", "t1"
    s.poll_interval, s.scheduler_tick = 0.1, 0.2
    env = tmp_path / "project" / ".env"  # what setup.sh generates: the stack template + deployment block
    env.parent.mkdir()
    env.write_text((ROOT / "stack" / "lightrag.env.tmpl.example").read_text().replace("__WEBUI_TITLE__", "'RAG · t1'")
                   .replace("__GENERATED__", "pw-generated") + DEPLOY_BLOCK)
    env.chmod(0o600)
    s.project_env, s.control_dir = str(env), str(tmp_path / "control")
    (tmp_path / "control").mkdir()
    app = create_app(s)
    with TestClient(app) as c:
        c.atlassian, c.lightrag, c.env, c.ctl = atlassian, mock_lightrag, env, tmp_path / "control"
        yield c
    server.should_exit = True
