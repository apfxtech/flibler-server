import os
from dataclasses import dataclass, field
from pathlib import Path

OFFICIAL_INDEX_URL = "https://update.flipperzero.one/firmware/directory.json"
UNLEASHED_INDEX_URL = "https://up.unleashedflip.com/directory.json"

def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]

@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8967
    data_dir: Path = Path.home() / ".flibler-server"
    shared_secret: str = ""
    app_name: str = "qUnleashed"
    auth_window_sec: int = 300
    allowed_bundle_hosts: list[str] = field(
        default_factory=lambda: ["catalog.flipperzero.one"]
    )
    allowed_index_urls: list[str] = field(
        default_factory=lambda: [UNLEASHED_INDEX_URL, OFFICIAL_INDEX_URL]
    )
    default_index_url: str = UNLEASHED_INDEX_URL
    cache_ttl_days: int = 7
    job_ttl_hours: int = 24
    queue_limit: int = 32
    build_timeout_sec: int = 600
    sdk_deploy_timeout_sec: int = 1800
    max_bundle_mb: int = 128
    sdk_refresh_hour_utc: int = 0
    sdk_profile_idle_days: int = 30

    @staticmethod
    def from_env() -> "Config":
        cfg = Config()
        env = os.environ
        cfg.host = env.get("QU_HOST", cfg.host)
        cfg.port = int(env.get("QU_PORT", cfg.port))
        cfg.data_dir = Path(env.get("QU_DATA_DIR", str(cfg.data_dir))).expanduser()
        cfg.shared_secret = env.get("QU_SHARED_SECRET", "")
        cfg.app_name = env.get("QU_APP_NAME", cfg.app_name)
        cfg.auth_window_sec = int(env.get("QU_AUTH_WINDOW_SEC", cfg.auth_window_sec))
        if hosts := env.get("QU_ALLOWED_BUNDLE_HOSTS"):
            cfg.allowed_bundle_hosts = _csv(hosts)
        if urls := env.get("QU_ALLOWED_INDEX_URLS"):
            cfg.allowed_index_urls = _csv(urls)
        cfg.default_index_url = env.get("QU_DEFAULT_INDEX_URL", cfg.default_index_url)
        cfg.cache_ttl_days = int(env.get("QU_CACHE_TTL_DAYS", cfg.cache_ttl_days))
        cfg.job_ttl_hours = int(env.get("QU_JOB_TTL_HOURS", cfg.job_ttl_hours))
        cfg.queue_limit = int(env.get("QU_QUEUE_LIMIT", cfg.queue_limit))
        cfg.build_timeout_sec = int(env.get("QU_BUILD_TIMEOUT_SEC", cfg.build_timeout_sec))
        cfg.sdk_deploy_timeout_sec = int(
            env.get("QU_SDK_DEPLOY_TIMEOUT_SEC", cfg.sdk_deploy_timeout_sec)
        )
        cfg.max_bundle_mb = int(env.get("QU_MAX_BUNDLE_MB", cfg.max_bundle_mb))
        cfg.sdk_refresh_hour_utc = int(
            env.get("QU_SDK_REFRESH_HOUR_UTC", cfg.sdk_refresh_hour_utc)
        )
        cfg.sdk_profile_idle_days = int(
            env.get("QU_SDK_PROFILE_IDLE_DAYS", cfg.sdk_profile_idle_days)
        )
        if not cfg.shared_secret:
            raise RuntimeError("QU_SHARED_SECRET is required")
        return cfg
