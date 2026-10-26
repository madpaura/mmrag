import os


def _b(v: str) -> str:
    v = v.strip()
    return "/" + v.strip("/") if v.strip("/") else ""


class Settings:
    def __init__(self) -> None:
        e = os.environ.get
        self.project = e("PROJECT_NAME", "dev")
        self.lightrag_url = e("LIGHTRAG_URL", "http://localhost:9621").rstrip("/")
        self.lightrag_api_key = e("LIGHTRAG_API_KEY") or None
        self.public_api_url = e("LIGHT_RAG_URL", "")
        self.base_path = _b(e("PORTAL_BASE_PATH", ""))
        self.data_dir = e("PORTAL_DATA_DIR", "./data")
        self.poll_interval = float(e("PORTAL_POLL_INTERVAL", "3"))
        self.monitor_timeout = float(e("PORTAL_MONITOR_TIMEOUT", str(6 * 3600)))
        self.max_parallel_runs = int(e("PORTAL_MAX_RUNS", "2"))
        self.conflict_wait = float(e("PORTAL_CONFLICT_WAIT", "900"))  # s to wait for LightRAG to finish deleting an old version
        self.scheduler_tick = float(e("PORTAL_SCHEDULER_TICK", "30"))
        self.stats_timeout = float(e("PORTAL_STATS_TIMEOUT", "5"))  # s; header status must not stall page loads
        self.log_tail = int(e("PORTAL_LOG_TAIL", "500"))  # run log lines shown on the run page
        # LightRAG configuration page: the project's .env (shared with setup.sh) and rag-app's control dir
        self.project_env = e("PORTAL_PROJECT_ENV", "")
        self.control_dir = e("PORTAL_CONTROL_DIR", "")
