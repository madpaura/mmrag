"""LightRAG configuration page: the editable settings, reading/writing the project's .env, restart control.

The project's .env (projects/<proj>/.env) stays the single source of truth, shared with setup.sh. rag-app loads it
on every start (stack/lightrag_run.py); restarting goes through a token file in the shared control directory.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

# Kept in sync with LightRAG v1.5.2 (api/config.py, constants.py, parser/registry.py).
PARSER_ENGINES = ("legacy", "native", "mineru", "docling")
PROCESS_OPTIONS = set("ite!FRVP")
CHUNKERS = set("FRVP")
MASK = "••••••••"


@dataclass
class Field:
    key: str
    label: str
    type: str = "text"  # text | number | float | bool | select | secret | json | parser
    help: str = ""
    default: str = ""  # what LightRAG uses when the key is not set
    options: list[tuple[str, str]] = field(default_factory=list)  # (value, label) for selects
    wide: bool = False
    warn: str = ""  # shown next to the field when its value changes


def _sel(*vals: str) -> list[tuple[str, str]]:
    return [(v, v) for v in vals]


GROUPS: list[tuple[str, str, str, list[Field]]] = [
    ("graph", "Knowledge graph", "How LightRAG turns chunks into entities and relations.", [
        Field("MMRAG_SKIP_KG", "Knowledge graph", "select", default="false",
              options=[("false", "Build the knowledge graph (entities and relations)"),
                       ("true", "Off: chunks and vectors only")],
              help="Off skips entity/relation extraction for every new or re-ingested document (Confluence, Jira "
                   "and uploads). Ingestion is much faster and makes no extraction LLM calls, but local, global "
                   "and hybrid queries find nothing; use naive or mix. Documents already ingested keep their "
                   "graph until they are re-ingested."),
        Field("MAX_GLEANING", "Gleaning passes", "number", default="1",
              help="Extra extraction passes per chunk to catch missed entities. 0 = single pass (faster)."),
        Field("MAX_EXTRACTION_ENTITIES", "Max entities per chunk", "number", default="40"),
        Field("MAX_EXTRACTION_RECORDS", "Max records per chunk", "number", default="100",
              help="Entities plus relations the LLM may return for one chunk."),
        Field("FORCE_LLM_SUMMARY_ON_MERGE", "Summarise after N descriptions", "number", default="8",
              help="When an entity collects this many descriptions, the LLM merges them into one summary."),
        Field("ENTITY_EXTRACTION_USE_JSON", "JSON extraction output", "bool", default="false",
              help="Ask the LLM for JSON instead of the delimiter format. More robust with models that follow "
                   "JSON well."),
        Field("SUMMARY_LANGUAGE", "Summary language", default="English",
              help="Language of entity descriptions and summaries."),
    ]),
    ("llm", "LLM", "The model that extracts entities and answers queries (via LiteLLM by default).", [
        Field("LLM_BINDING", "Binding", "select", default="ollama",
              options=_sel("openai", "ollama", "openai-ollama", "azure_openai", "bedrock", "gemini", "lollms")),
        Field("LLM_MODEL", "Model", wide=True),
        Field("LLM_BINDING_HOST", "Endpoint", wide=True, help="Containers reach the host as host.docker.internal."),
        Field("LLM_BINDING_API_KEY", "API key", "secret"),
        Field("EXTRACT_LLM_MODEL", "Extraction model", wide=True,
              help="Model for entity extraction only. Empty = the model above."),
        Field("SUMMARY_LLM_MODEL", "Summary model", wide=True,
              help="Model for merging entity descriptions. Empty = the model above."),
        Field("MAX_ASYNC_LLM", "Concurrent LLM calls", "number", help="Empty = MAX_ASYNC below."),
        Field("EXTRACT_MAX_ASYNC_LLM", "Concurrent extraction calls", "number", help="Empty = the value above."),
        Field("MAX_ASYNC", "Concurrency fallback (MAX_ASYNC)", "number", default="4",
              help="Used where no specific limit is set."),
        Field("LLM_TIMEOUT", "Timeout (s)", "number", default="240"),
        Field("OPENAI_LLM_TEMPERATURE", "Temperature", "float"),
        Field("OPENAI_LLM_EXTRA_BODY", "Extra request body (JSON)", "json", wide=True,
              help='Merged into every OpenAI-compatible request, e.g. {"chat_template_kwargs": {"enable_thinking": false}}.'),
    ]),
    ("embedding", "Embeddings", "Vectors for chunks, entities and relations.", [
        Field("EMBEDDING_BINDING", "Binding", "select", default="ollama",
              options=_sel("openai", "ollama", "azure_openai", "bedrock", "jina", "gemini", "voyageai", "lollms")),
        Field("EMBEDDING_MODEL", "Model", wide=True,
              warn="Changing the embedding model makes existing vectors meaningless: clear all data and re-ingest."),
        Field("EMBEDDING_DIM", "Dimension", "number",
              warn="Must match the model. Changing it breaks the existing vector tables: clear all data first."),
        Field("EMBEDDING_BINDING_HOST", "Endpoint", wide=True),
        Field("EMBEDDING_BINDING_API_KEY", "API key", "secret"),
        Field("EMBEDDING_BATCH_NUM", "Batch size", "number", default="10"),
        Field("EMBEDDING_FUNC_MAX_ASYNC", "Concurrent embedding calls", "number", default="8"),
    ]),
    ("processing", "Document processing", "Parsing and chunking of new documents.", [
        Field("LIGHTRAG_PARSER", "Parser rules", "parser", wide=True, default="*:legacy",
              help="Comma-separated <suffix>:<engine>[-options], first match wins. Engines: legacy, native, mineru, "
                   "docling. Options: i/t/e = VLM for images/tables/equations, ! = no knowledge graph, "
                   "F/R/V/P = fixed/recursive/vector/paragraph chunking. Example: pdf:mineru-iR,*:legacy-R. "
                   "Applies to uploaded files only."),
        Field("CHUNK_SIZE", "Chunk size (tokens)", "number", default="1200"),
        Field("CHUNK_OVERLAP_SIZE", "Chunk overlap (tokens)", "number", default="100"),
        Field("MAX_PARALLEL_INSERT", "Documents in parallel", "number", default="3"),
        Field("VLM_PROCESS_ENABLE", "Analyse images with a VLM", "bool", default="false"),
        Field("VLM_LLM_MODEL", "VLM model", wide=True, help="Empty = the LLM model."),
        Field("MINERU_LOCAL_ENDPOINT", "MinerU endpoint", wide=True, help="Used by mineru parser rules."),
    ]),
    ("query", "Queries", "Retrieval defaults for /query and the Ask page.", [
        Field("TOP_K", "Top K entities/relations", "number", default="40"),
        Field("CHUNK_TOP_K", "Top K chunks", "number", default="20"),
        Field("MAX_TOTAL_TOKENS", "Max context tokens", "number", default="30000"),
        Field("ENABLE_LLM_CACHE", "Cache query answers", "bool", default="true"),
        Field("ENABLE_LLM_CACHE_FOR_EXTRACT", "Cache extraction results", "bool", default="true",
              help="Re-ingesting an unchanged chunk reuses the earlier extraction instead of calling the LLM."),
    ]),
    ("server", "Server", "", [
        Field("WEBUI_TITLE", "Web UI title", wide=True),
        Field("WEBUI_DESCRIPTION", "Web UI description", wide=True),
        Field("LOG_LEVEL", "Log level", "select", default="INFO", options=_sel("DEBUG", "INFO", "WARNING", "ERROR")),
    ]),
]
FIELDS = {f.key: f for _, _, _, fs in GROUPS for f in fs}

# Set by setup.sh / docker-compose, or deployment plumbing that must not change from a browser.
PROTECTED = {"PROJECT", "BASE_URL", "API_PREFIX", "MGMT_PREFIX", "LIGHT_RAG_URL", "LIGHT_RAG_MGMT_URL", "LIGHTRAG_PORT",
             "PORTAL_PORT", "LIGHTRAG_IMAGE", "LIGHTRAG_API_KEY", "MMRAG_SESSION", "WHITELIST_PATHS", "HOST", "PORT",
             "WORKING_DIR", "INPUT_DIR", "PROMPT_DIR", "LIGHTRAG_API_PREFIX", "LIGHTRAG_KV_STORAGE",
             "LIGHTRAG_VECTOR_STORAGE", "LIGHTRAG_DOC_STATUS_STORAGE", "LIGHTRAG_GRAPH_STORAGE", "AUTH_ACCOUNTS",
             "TOKEN_SECRET", "WORKSPACE"}
PROTECTED_PREFIXES = ("POSTGRES_", "NEO4J_")
SECRET_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|SESSION)$")
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
SAFE_RE = re.compile(r"^[A-Za-z0-9_./:@,+=*%!~-]*$")


class ConfigError(ValueError):
    pass


def protected(key: str) -> bool:
    return key in PROTECTED or key.startswith(PROTECTED_PREFIXES)


def secret(key: str) -> bool:
    return bool(SECRET_RE.search(key)) or (key in FIELDS and FIELDS[key].type == "secret")


def fmt_value(key: str, v: str) -> str:
    """Quote a value so python-dotenv (rag-app) and docker compose (interpolation) read it back unchanged."""
    if "\n" in v or "\r" in v:
        raise ConfigError(f"{key}: line breaks are not allowed")
    if SAFE_RE.match(v):
        return v
    if "'" in v:
        raise ConfigError(f"{key}: single quotes (') are not allowed in values")
    return f"'{v}'"


def check_parser(rules: str) -> None:
    for i, item in enumerate((x.strip() for x in re.split(r"[;,]", rules)), 1):
        if not item:
            continue
        pat, sep, rhs = item.partition(":")
        if not sep or not pat.strip() or not rhs.strip():
            raise ConfigError(f"Parser rules: rule {i} ({item!r}) must be <suffix>:<engine>[-options]")
        if "." in pat:
            raise ConfigError(f"Parser rules: rule {i} ({item!r}): write suffixes without dots (pdf, not *.pdf)")
        engine, _, opts = rhs.strip().partition("-")
        if engine.lower() not in PARSER_ENGINES:
            raise ConfigError(f"Parser rules: rule {i} ({item!r}): unknown engine {engine!r}; "
                              f"use one of {', '.join(PARSER_ENGINES)}")
        bad = sorted(set(opts.replace(" ", "")) - PROCESS_OPTIONS)
        if bad:
            raise ConfigError(f"Parser rules: rule {i} ({item!r}): unknown option(s) {''.join(bad)!r}")
        if len(set(opts) & CHUNKERS) > 1:
            raise ConfigError(f"Parser rules: rule {i} ({item!r}): pick one chunking mode of F/R/V/P")


def check(f: Field, v: str) -> str:
    v = v.strip()
    if v == "":
        return v
    if f.type == "number":
        if not re.fullmatch(r"\d+", v):
            raise ConfigError(f"{f.label}: whole number expected (got {v!r})")
    elif f.type == "float":
        try:
            float(v)
        except ValueError:
            raise ConfigError(f"{f.label}: number expected (got {v!r})") from None
    elif f.type == "bool":
        v = "true" if v.lower() in ("true", "1", "yes", "on") else "false"
    elif f.type == "select" and v not in [o for o, _ in f.options]:
        raise ConfigError(f"{f.label}: choose one of {', '.join(o for o, _ in f.options)}")
    elif f.type == "json":
        try:
            json.loads(v)
        except ValueError as e:
            raise ConfigError(f"{f.label}: invalid JSON ({e})") from None
    elif f.type == "parser":
        check_parser(v)
    return v


class EnvFile:
    """The project's .env: values via python-dotenv (as rag-app reads them), edits that keep comments and order."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def available(self) -> bool:
        return self.path.is_file()

    def raw(self) -> bytes:
        return self.path.read_bytes()

    def sha256(self) -> str:
        return hashlib.sha256(self.raw()).hexdigest()

    def values(self) -> dict[str, str]:
        return {k: v or "" for k, v in dotenv_values(stream=io.StringIO(self.raw().decode("utf-8"))).items()}

    def update(self, changes: dict[str, str | None]) -> None:
        """Set (str) or remove (None) keys. Existing lines are replaced in place, new keys appended."""
        lines = self.raw().decode("utf-8").splitlines()
        out, done = [], set()
        for line in lines:
            m = LINE_RE.match(line)
            k = m.group(1) if m else None
            if k in changes:
                if k in done or changes[k] is None:
                    continue  # drop duplicates (dotenv's last-wins would undo the edit) and removed keys
                line = f"{k}={fmt_value(k, changes[k])}"
                done.add(k)
            out.append(line)
        new = [k for k, v in changes.items() if v is not None and k not in done]
        if new:
            if not any(l.startswith("# ── set in the portal") for l in out):
                out += ["", "# ── set in the portal (LightRAG page) ──"]
            out += [f"{k}={fmt_value(k, changes[k])}" for k in new]
        self._write("\n".join(out) + "\n")

    def _write(self, text: str) -> None:
        """Atomic replace that keeps the owner and mode: setup.sh runs as the host user and the file holds secrets."""
        st = self.path.stat()
        tmp = self.path.with_name(f".{self.path.name}.portal-tmp")
        tmp.write_text(text, "utf-8")
        os.chmod(tmp, st.st_mode & 0o777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass
        os.replace(tmp, self.path)


def form_changes(env: dict[str, str], form) -> dict[str, str | None]:
    """Validated changes from the form: known fields plus the 'other variables' text box."""
    changes: dict[str, str | None] = {}
    for key, f in FIELDS.items():
        if f.type == "bool":
            if f"{key}__present" not in form:
                continue
            v = "true" if form.get(key) else "false"
        elif key not in form:
            continue
        else:
            v = check(f, str(form.get(key, "")))
            if f.type == "secret" and v == "":
                continue  # blank = keep the stored secret
        if f.type in ("bool", "select") and key not in env and v == f.default:
            continue  # left at LightRAG's default: no line needed
        if v == "" and key not in env:
            continue
        if env.get(key) != v:
            changes[key] = None if v == "" else v
    if "__other" in form:
        changes.update(other_changes(env, str(form.get("__other", ""))))
    return changes


def other_vars(env: dict[str, str]) -> list[tuple[str, str]]:
    return [(k, v) for k, v in env.items() if k not in FIELDS and not protected(k)]


def other_text(env: dict[str, str]) -> str:
    return "\n".join(f"{k}={MASK if secret(k) and v else v}" for k, v in other_vars(env))


def other_changes(env: dict[str, str], text: str) -> dict[str, str | None]:
    want: dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, sep, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        if not sep or not KEY_RE.match(k):
            raise ConfigError(f"Other variables, line {n}: expected KEY=value (got {line!r})")
        if protected(k):
            raise ConfigError(f"Other variables: {k} is managed by setup.sh and can't be changed here")
        if k in FIELDS:
            raise ConfigError(f"Other variables: {k} has its own field above")
        if k == "ENTITY_TYPES":
            raise ConfigError("Other variables: ENTITY_TYPES is not supported by LightRAG 1.5 (it refuses to start)")
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        if v == MASK and k in env:
            v = env[k]
        want[k] = v
    changes: dict[str, str | None] = {k: v for k, v in want.items() if env.get(k) != v}
    for k, _ in other_vars(env):
        if k not in want:
            changes[k] = None
    return changes


class Control:
    """rag-app's control directory: status.json from stack/lightrag_run.py, restart requests from us."""

    def __init__(self, path: str | Path):
        self.path = Path(path) if path else None

    def available(self) -> bool:
        return bool(self.path) and self.path.is_dir()

    def status(self) -> dict | None:
        try:
            return json.loads((self.path / "status.json").read_text())
        except (OSError, ValueError, TypeError):
            return None

    def requested(self) -> str:
        try:
            return (self.path / "restart").read_text().strip()
        except (OSError, TypeError):
            return ""

    def request_restart(self) -> str:
        token = f"{time.time():.6f}"
        tmp = self.path / ".restart.tmp"
        tmp.write_text(token)
        os.replace(tmp, self.path / "restart")
        return token
