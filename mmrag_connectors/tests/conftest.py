import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "simulators"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return server, port


@pytest.fixture(scope="session")
def atlassian():
    import mock_atlassian
    server, port = _serve(mock_atlassian.app)
    mock_atlassian.BASE = f"http://127.0.0.1:{port}"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture()
def lightrag_url():
    import mock_lightrag
    mock_lightrag.DOCS.clear()
    mock_lightrag.DELAY = 0.3
    server, port = _serve(mock_lightrag.app)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
