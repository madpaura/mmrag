"""LightRAG settings page (.env editing, restart control) and the rag-app entrypoint (stack/lightrag_run.py)."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from dotenv import dotenv_values

B = "/mmrag/t1/mgmt"
ROOT = Path(__file__).resolve().parents[2]


def values(c):
    return dotenv_values(c.env)


def save(c, data, action="save"):
    return c.post(f"{B}/lightrag", data={**data, "action": action}, follow_redirects=False)


def running(c, **kw):
    """Pretend rag-app's entrypoint is up and loaded the current .env."""
    import hashlib
    st = {"state": "running", "started_at": time.time(), "env_sha256": hashlib.sha256(c.env.read_bytes()).hexdigest(),
          "restart_token": "", **kw}
    (c.ctl / "status.json").write_text(json.dumps(st))


def test_page_shows_values_masks_secrets_and_deployment(stack):
    r = stack.get(f"{B}/lightrag")
    assert r.status_code == 200
    t = r.text
    assert 'value="qwen3.8-flash-next"' in t and "LIGHTRAG_PARSER" in t and "Knowledge graph" in t
    assert "__LITELLM_API_KEY__" not in t and "mmr_secretkey" not in t and "pw-generated" not in t
    assert "mmr_sessionsecret" not in t and "MMRAG_SESSION" in t
    assert "Stored · leave blank to keep" in t
    assert "LIGHTRAG_PORT" in t and "19606" in t  # deployment block, read-only
    assert 'name="LIGHTRAG_PORT"' not in t
    assert 'href="/mmrag/t1/mgmt/lightrag"' in stack.get(f"{B}/").text  # tab


def test_save_writes_env_in_place_and_keeps_the_rest(stack):
    before = stack.env.read_text()
    r = save(stack, {"MMRAG_SKIP_KG": "true", "CHUNK_SIZE": "800", "LLM_BINDING_API_KEY": "",
                     "WEBUI_TITLE": "Firmware wiki · RAG", "OPENAI_LLM_EXTRA_BODY": '{"a": {"b": false}}',
                     "ENABLE_LLM_CACHE__present": "1"})
    assert r.status_code == 303 and "Restart" in r.headers["location"]
    vals = values(stack)
    assert vals["MMRAG_SKIP_KG"] == "true" and vals["CHUNK_SIZE"] == "800"
    assert vals["LLM_BINDING_API_KEY"] == "__LITELLM_API_KEY__"  # blank secret = kept
    assert vals["WEBUI_TITLE"] == "Firmware wiki · RAG" and json.loads(vals["OPENAI_LLM_EXTRA_BODY"]) == {"a": {"b": False}}
    assert vals["ENABLE_LLM_CACHE"] == "false"  # unchecked box
    assert vals["LIGHTRAG_API_KEY"] == "mmr_secretkey" and vals["POSTGRES_PASSWORD"] == "pw-generated"
    after = stack.env.read_text()
    assert "# ── Neo4j (Graph) ──" in after and "CHUNK_SIZE=800\n" in after
    assert after.splitlines().index("CHUNK_SIZE=800") == before.splitlines().index("CHUNK_SIZE=1200")  # in place
    assert oct(stack.env.stat().st_mode & 0o777) == "0o600"
    # untouched fields don't produce writes; no-op save says so
    r = save(stack, {"CHUNK_SIZE": "800"})
    assert "No+changes" in r.headers["location"] or "No%20changes" in r.headers["location"]


def test_new_key_appended_and_cleared_field_removed(stack):
    save(stack, {"MAX_TOTAL_TOKENS": "24000"})
    assert values(stack)["MAX_TOTAL_TOKENS"] == "24000"
    assert "# ── set in the portal" in stack.env.read_text()
    save(stack, {"MAX_TOTAL_TOKENS": "", "TOP_K": ""})
    vals = values(stack)
    assert "MAX_TOTAL_TOKENS" not in vals and "TOP_K" not in vals  # back to LightRAG's default
    # a select left at its default doesn't add a line
    save(stack, {"LOG_LEVEL": "INFO"})
    assert "LOG_LEVEL" not in values(stack)


def test_validation_rejects_and_leaves_file_alone(stack):
    before = stack.env.read_bytes()
    for data, msg in [({"CHUNK_SIZE": "12k"}, "whole number"),
                      ({"LIGHTRAG_PARSER": "*:fancy"}, "unknown engine"),
                      ({"LIGHTRAG_PARSER": "*.pdf:legacy"}, "without dots"),
                      ({"LIGHTRAG_PARSER": "*:legacy-RF"}, "one chunking mode"),
                      ({"OPENAI_LLM_EXTRA_BODY": "{nope"}, "invalid JSON"),
                      ({"WEBUI_TITLE": "it's"}, "single quotes"),
                      ({"LLM_BINDING": "skynet"}, "choose one of"),
                      ({"__other": "POSTGRES_PASSWORD=x"}, "managed by setup.sh"),
                      ({"__other": "LIGHTRAG_API_KEY=x"}, "managed by setup.sh"),
                      ({"__other": "CHUNK_SIZE=5"}, "own field"),
                      ({"__other": "ENTITY_TYPES=a,b"}, "not supported"),
                      ({"__other": "not a line"}, "KEY=value")]:
        r = save(stack, {"CHUNK_SIZE": "1200", **data})
        assert r.status_code == 422 and msg in r.text, (data, r.text[-500:])
        assert 'value="12k"' in r.text if data.get("CHUNK_SIZE") == "12k" else True  # form keeps the input
    assert stack.env.read_bytes() == before
    save(stack, {"LIGHTRAG_PARSER": "pdf:mineru-iR!, *:legacy-R"})
    assert values(stack)["LIGHTRAG_PARSER"] == "pdf:mineru-iR!, *:legacy-R"


def test_other_variables_add_remove_and_keep_masked_secret(stack):
    page = stack.get(f"{B}/lightrag").text
    assert "MINERU_MAX_POLLS=3600" in page
    save(stack, {"__other": "RERANK_BINDING_API_KEY=sk-rerank\nMINERU_MAX_POLLS=10"})
    vals = values(stack)
    assert vals["RERANK_BINDING_API_KEY"] == "sk-rerank" and vals["MINERU_MAX_POLLS"] == "10"
    assert "MINERU_POLL_INTERVAL_SECONDS" not in vals  # deleted line = unset
    assert vals["PROJECT"] == "t1"  # protected keys are never in the box, so never removed
    page = stack.get(f"{B}/lightrag").text
    assert "sk-rerank" not in page and "RERANK_BINDING_API_KEY=••••••••" in page
    save(stack, {"__other": "RERANK_BINDING_API_KEY=••••••••\nMINERU_MAX_POLLS=10"})
    assert values(stack)["RERANK_BINDING_API_KEY"] == "sk-rerank"


def test_restart_needs_the_entrypoint_then_signals_it(stack):
    r = stack.post(f"{B}/lightrag/restart")
    assert r.status_code == 409 and "setup.sh deploy" in r.text
    assert "isn't enabled for this project" in stack.get(f"{B}/lightrag").text
    running(stack)
    t = stack.get(f"{B}/lightrag/status").text
    assert 'data-state="running"' in t and "data-pending" not in t
    r = save(stack, {"MMRAG_SKIP_KG": "true"}, action="restart")
    assert r.status_code == 303 and "restarting" in r.headers["location"]
    token = (stack.ctl / "restart").read_text()
    assert token
    assert 'data-state="restarting"' in stack.get(f"{B}/lightrag/status").text
    # the entrypoint came back with the new .env
    running(stack, restart_token=token)
    t = stack.get(f"{B}/lightrag/status").text
    assert 'data-state="running"' in t and "data-pending" not in t
    # a plain save afterwards is flagged as not applied yet
    save(stack, {"CHUNK_SIZE": "900"})
    assert "data-pending" in stack.get(f"{B}/lightrag/status").text


def dead_portal(tmp_path, env=None, ctl=None):
    """Portal whose LightRAG doesn't answer (stopped or still booting)."""
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    s = Settings()
    s.data_dir, s.lightrag_url, s.base_path, s.project = str(tmp_path / "d"), "http://127.0.0.1:9", B, "t1"
    s.project_env, s.control_dir = str(env or ""), str(ctl or "")
    return TestClient(create_app(s))


def test_starting_and_exited_states(stack, tmp_path):
    running(stack)
    with dead_portal(tmp_path, stack.env, stack.ctl) as c:
        t = c.get(f"{B}/lightrag/status").text
        assert 'data-state="starting"' in t and 'every 2s' in t  # polls quickly while it boots
        running(stack, state="exited", returncode=3)
        t = c.get(f"{B}/lightrag/status").text
        assert 'data-state="exited"' in t and "code 3" in t and "Restart LightRAG" in t


def test_ask_defaults_to_naive_without_graph(stack):
    assert 'id="mode" value="mix"' in stack.get(f"{B}/ask").text
    save(stack, {"MMRAG_SKIP_KG": "true"})
    t = stack.get(f"{B}/ask").text
    assert 'id="mode" value="naive"' in t and "knowledge graph is off" in t
    assert 'id="mode" value="local"' in stack.get(f"{B}/ask?mode=local").text


def test_read_only_without_mounted_env(tmp_path):
    with dead_portal(tmp_path) as c:
        t = c.get(f"{B}/lightrag").text
        assert "Read-only" in t and "./setup.sh deploy t1" in t
        assert c.post(f"{B}/lightrag", data={"CHUNK_SIZE": "1"}).status_code == 409
        assert c.post(f"{B}/lightrag/restart").status_code == 409


# ---- stack/lightrag_run.py ----------------------------------------------------
CHILD = ("import json,os,sys,time,signal;"
         "signal.signal(signal.SIGTERM, lambda *a: sys.exit(7));"
         "open(sys.argv[1],'w').write(json.dumps(dict(os.environ)));"
         "time.sleep(60)")


def start_wrapper(tmp_path, env_text):
    env = tmp_path / ".env"
    env.write_text(env_text)
    ctl = tmp_path / "ctl"
    out = tmp_path / "child_env.json"
    e = {**os.environ, "MMRAG_ENV_FILE": str(env), "MMRAG_CONTROL_DIR": str(ctl), "MMRAG_PINNED_ENV": "PORT,HOST",
         "PORT": "9621", "MMRAG_LIGHTRAG_CMD": f"{sys.executable} -c \"{CHILD}\" {out}", "MMRAG_STOP_TIMEOUT": "5"}
    p = subprocess.Popen([sys.executable, str(ROOT / "stack" / "lightrag_run.py")], env=e)
    end = time.time() + 10
    while not out.exists() and time.time() < end:
        time.sleep(0.05)
    time.sleep(0.1)
    return p, ctl, json.loads(out.read_text())


def test_wrapper_loads_env_and_restarts_on_request(tmp_path):
    p, ctl, child_env = start_wrapper(tmp_path, "LLM_MODEL=m1\nPORT=1111\nWEBUI_TITLE='RAG · x'\n"
                                                "OPENAI_LLM_EXTRA_BODY='{\"a\": 1}'\nMMRAG_SKIP_KG=true\n")
    try:
        assert child_env["LLM_MODEL"] == "m1" and child_env["MMRAG_SKIP_KG"] == "true"
        assert child_env["PORT"] == "9621"  # pinned by compose
        assert child_env["WEBUI_TITLE"] == "RAG · x" and child_env["OPENAI_LLM_EXTRA_BODY"] == '{"a": 1}'
        st = json.loads((ctl / "status.json").read_text())
        import hashlib
        assert st["state"] == "running" and st["env_sha256"] == hashlib.sha256((tmp_path / ".env").read_bytes()).hexdigest()
        (ctl / "restart").write_text("tok-1")
        assert p.wait(10) == 0  # exits so Docker restarts the container
        assert json.loads((ctl / "status.json").read_text())["state"] == "restarting"
    finally:
        p.kill()


def test_wrapper_ignores_old_token_and_forwards_sigterm(tmp_path):
    (tmp_path / "ctl").mkdir()
    (tmp_path / "ctl" / "restart").write_text("old")
    p, ctl, _ = start_wrapper(tmp_path, "A=1\n")
    try:
        time.sleep(1.5)
        assert p.poll() is None  # a request from before the start is already satisfied
        assert json.loads((ctl / "status.json").read_text())["restart_token"] == "old"
        p.send_signal(signal.SIGTERM)
        assert p.wait(10) == 7  # docker stop: child's exit code passes through, no restart
        assert json.loads((ctl / "status.json").read_text())["state"] == "exited"
    finally:
        p.kill()
