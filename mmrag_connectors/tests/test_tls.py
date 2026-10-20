"""Company-CA trust: an HTTPS Confluence mock signed by a private root -> intermediate -> leaf chain."""
import socket
import subprocess
import threading
import time

import pytest
import uvicorn

from mmrag_connectors import ConnectorError, get


def _sh(*a, cwd):
    subprocess.run(a, cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    (d / "int.ext").write_text("basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n")
    (d / "leaf.ext").write_text("basicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n")
    _sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "root.key", "-out", "root.pem", "-days", "30",
        "-subj", "/CN=Test Root CA", "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=d)
    _sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "int.key", "-out", "int.csr", "-subj", "/CN=Test Issuing CA", cwd=d)
    _sh("openssl", "x509", "-req", "-in", "int.csr", "-CA", "root.pem", "-CAkey", "root.key", "-CAcreateserial", "-out", "int.pem",
        "-days", "30", "-extfile", "int.ext", cwd=d)
    _sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr", "-subj", "/CN=localhost", cwd=d)
    _sh("openssl", "x509", "-req", "-in", "leaf.csr", "-CA", "int.pem", "-CAkey", "int.key", "-CAcreateserial", "-out", "leaf.pem",
        "-days", "30", "-extfile", "leaf.ext", cwd=d)
    (d / "chain.pem").write_text((d / "leaf.pem").read_text() + (d / "int.pem").read_text())  # server omits the root
    return d


@pytest.fixture(scope="module")
def https_url(pki):
    import mock_atlassian
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(mock_atlassian.app, host="127.0.0.1", port=port, log_level="error",
                                        ssl_certfile=str(pki / "chain.pem"), ssl_keyfile=str(pki / "leaf.key")))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    yield f"https://127.0.0.1:{port}/confluence"
    srv.should_exit = True


def _conf(url, verify=True):
    return get("confluence")({"base_url": url, "token": "sim-token", "roots": "1000", "depth": 1, "verify_ssl": verify})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("MMRAG_CA_BUNDLE", "MMRAG_CA_DIR", "SSL_CERT_FILE"):
        monkeypatch.delenv(v, raising=False)


def test_company_ca_fails_with_actionable_message(https_url):
    with pytest.raises(ConnectorError) as e:
        _conf(https_url).test()
    assert "unable to get local issuer certificate" in str(e.value) and "./setup.sh trust-ca" in str(e.value)
    with pytest.raises(ConnectorError):  # a run fails too instead of 'completing' with 0 documents
        list(_conf(https_url).fetch())


def test_trusting_the_root_fixes_it(https_url, pki, monkeypatch):
    monkeypatch.setenv("MMRAG_CA_BUNDLE", str(pki / "root.pem"))
    assert "ok" in _conf(https_url).test()
    assert len(list(_conf(https_url).fetch())) == 1


def test_trusting_only_the_intermediate_is_enough(https_url, pki, monkeypatch):
    d = pki / "cadir"
    d.mkdir(exist_ok=True)
    (d / "issuing.crt").write_text((pki / "int.pem").read_text())
    monkeypatch.setenv("MMRAG_CA_DIR", str(d))           # how the portal container gets config/ca
    assert "ok" in _conf(https_url).test()


def test_verify_off_still_possible(https_url):
    assert "ok" in _conf(https_url, verify=False).test()
