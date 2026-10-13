import os
from dataclasses import dataclass, field
from pathlib import Path

OFFICIAL_INDEX_URL = "https://update.flipperzero.one/firmware/directory.json"
UNLEASHED_INDEX_URL = "https://up.unleashedflip.com/directory.json"

OFW_CATALOG_REPO = "https://github.com/flipperdevices/flipper-application-catalog.git"

FAMILIES = ("application", "menustyle", "animation")


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8967
    data_dir: Path = Path.home() / ".flibler-server"
    public_base_url: str = ""
    shared_secret: str = ""
    ci_token: str = ""
    admin_token: str = ""
    rate_limit_per_min: int = 600
    app_name: str = "qUnleashed"
    auth_window_sec: int = 300

    # Sources
    ofw_catalog_repo: str = OFW_CATALOG_REPO
    menustyles_repo: str = ""
    animations_repo: str = ""
    allow_file_origins: bool = False  # tests only
    trust_proxy: bool = False
    cors_origins: list[str] = field(default_factory=list)
    ban_days: int = 1
    allowed_bundle_hosts: list[str] = field(default_factory=list)
    source_check_hour_utc: int = 0

    # Archive
    archive_mirror_base: str = ""
    archive_git_user: str = "qUnleashed catalog"
    archive_git_email: str = "catalog@qunleashed"

    # SDK
    sdk_index_url: str = UNLEASHED_INDEX_URL
    sdk_channels: list[str] = field(default_factory=lambda: ["release", "dev"])
    bootstrap: bool = True
    sdk_active_days: int = 30
    sdk_deploy_timeout_sec: int = 1800

    # Builds
    cache_ttl_days: int = 7
    job_ttl_hours: int = 24
    queue_limit: int = 256
    build_timeout_sec: int = 600
    max_bundle_mb: int = 128
    max_unpack_mb: int = 512
    build_command_prefix: list[str] = field(default_factory=list)
    workers: int = 1
    top_keep_warm: int = 50

    # Signing
    signing_key_hex: str = ""
    signing_kid: str = "k1"

    # Push
    fcm_service_account: str = ""
    push_digest_threshold: int = 3
    push_debounce_sec: float = 300.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "catalog.sqlite"

    @property
    def archive_root(self) -> Path:
        return self.data_dir / "archive"

    @property
    def sdk_root(self) -> Path:
        return self.data_dir / "sdk"

    @property
    def cache_root(self) -> Path:
        return self.data_dir / "cache"

    @property
    def bundles_root(self) -> Path:
        return self.data_dir / "bundles"

    @property
    def assets_root(self) -> Path:
        return self.data_dir / "assets"

    @property
    def logs_root(self) -> Path:
        return self.data_dir / "logs"

    @property
    def work_root(self) -> Path:
        return self.data_dir / "work"

    @property
    def sources_root(self) -> Path:
        return self.data_dir / "sources"

    def bundle_hosts(self) -> set[str]:
        hosts = set(self.allowed_bundle_hosts)
        hosts.add("catalog.flipperzero.one")
        if self.public_base_url:
            from urllib.parse import urlparse

            host = urlparse(self.public_base_url).hostname
            if host:
                hosts.add(host)
        return hosts

    @staticmethod
    def from_env() -> "Config":
        cfg = Config()
        env = os.environ

        def text(name: str, default: str) -> str:
            return env.get(name, default)

        def integer(name: str, default: int) -> int:
            return int(env.get(name, default))

        cfg.host = text("QU_HOST", cfg.host)
        cfg.port = integer("QU_PORT", cfg.port)
        cfg.data_dir = Path(text("QU_DATA_DIR", str(cfg.data_dir))).expanduser()
        cfg.public_base_url = text("QU_PUBLIC_BASE_URL", cfg.public_base_url).rstrip("/")
        cfg.shared_secret = text("QU_SHARED_SECRET", "")
        cfg.ci_token = text("QU_CI_TOKEN", "")
        cfg.admin_token = text("QU_ADMIN_TOKEN", "")
        cfg.rate_limit_per_min = integer("QU_RATE_LIMIT_PER_MIN", cfg.rate_limit_per_min)
        cfg.app_name = text("QU_APP_NAME", cfg.app_name)
        cfg.auth_window_sec = integer("QU_AUTH_WINDOW_SEC", cfg.auth_window_sec)

        cfg.ofw_catalog_repo = text("QU_OFW_CATALOG_REPO", cfg.ofw_catalog_repo)
        cfg.menustyles_repo = text("QU_MENUSTYLES_REPO", cfg.menustyles_repo)
        cfg.animations_repo = text("QU_ANIMATIONS_REPO", cfg.animations_repo)
        cfg.trust_proxy = env.get("QU_TRUST_PROXY", "0") in ("1", "true", "yes")
        if origins := env.get("QU_CORS_ORIGINS"):
            cfg.cors_origins = _csv(origins)
        cfg.ban_days = integer("QU_BAN_DAYS", cfg.ban_days)
        if hosts := env.get("QU_ALLOWED_BUNDLE_HOSTS"):
            cfg.allowed_bundle_hosts = _csv(hosts)
        cfg.source_check_hour_utc = integer("QU_SOURCE_CHECK_HOUR_UTC", cfg.source_check_hour_utc)

        cfg.archive_mirror_base = text("QU_ARCHIVE_MIRROR_BASE", cfg.archive_mirror_base).rstrip("/")
        cfg.archive_git_user = text("QU_ARCHIVE_GIT_USER", cfg.archive_git_user)
        cfg.archive_git_email = text("QU_ARCHIVE_GIT_EMAIL", cfg.archive_git_email)

        cfg.sdk_index_url = text("QU_SDK_INDEX_URL", cfg.sdk_index_url)
        if channels := env.get("QU_SDK_CHANNELS"):
            cfg.sdk_channels = _csv(channels)
        cfg.bootstrap = env.get("QU_BOOTSTRAP", "1") not in ("0", "false", "no")
        cfg.sdk_active_days = integer("QU_SDK_ACTIVE_DAYS", cfg.sdk_active_days)
        cfg.sdk_deploy_timeout_sec = integer("QU_SDK_DEPLOY_TIMEOUT_SEC", cfg.sdk_deploy_timeout_sec)

        cfg.cache_ttl_days = integer("QU_CACHE_TTL_DAYS", cfg.cache_ttl_days)
        cfg.job_ttl_hours = integer("QU_JOB_TTL_HOURS", cfg.job_ttl_hours)
        cfg.queue_limit = integer("QU_QUEUE_LIMIT", cfg.queue_limit)
        cfg.build_timeout_sec = integer("QU_BUILD_TIMEOUT_SEC", cfg.build_timeout_sec)
        cfg.max_bundle_mb = integer("QU_MAX_BUNDLE_MB", cfg.max_bundle_mb)
        cfg.max_unpack_mb = integer("QU_MAX_UNPACK_MB", cfg.max_unpack_mb)
        if prefix := env.get("QU_BUILD_COMMAND_PREFIX"):
            cfg.build_command_prefix = prefix.split()
        cfg.workers = integer("QU_WORKERS", cfg.workers)
        cfg.top_keep_warm = integer("QU_TOP_KEEP_WARM", cfg.top_keep_warm)

        cfg.signing_key_hex = text("QU_SIGNING_KEY_HEX", "")
        cfg.signing_kid = text("QU_SIGNING_KID", cfg.signing_kid)

        cfg.fcm_service_account = text("QU_FCM_SERVICE_ACCOUNT", "")
        cfg.push_digest_threshold = integer("QU_PUSH_DIGEST_THRESHOLD", cfg.push_digest_threshold)
        cfg.push_debounce_sec = float(env.get("QU_PUSH_DEBOUNCE_SEC", cfg.push_debounce_sec))

        if not cfg.shared_secret:
            raise RuntimeError("QU_SHARED_SECRET is required")
        return cfg
