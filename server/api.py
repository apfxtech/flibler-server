import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .archive import Archive
from .builder import Builder
from .catalog import Catalog
from .config import FAMILIES, Config
from .db import Database
from .jobs import JobManager
from .push import PushSender
from .scheduler import Scheduler
from .sources.manager import SourceManager
from .stats import Stats
from .sdk_archive import SdkArchive
from .signing import Signer

log = logging.getLogger("catalog.api")


class AppState:
    """Everything the routers share. Built once in create_app and handed to
    each router builder explicitly."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(cfg.db_path)
        self.sdk = SdkArchive(cfg, self.db)
        self.builder = Builder(cfg)
        self.archives = {family: Archive(cfg, family) for family in FAMILIES}
        self.jobs = JobManager(cfg, self.db, self.sdk, self.builder, self.archives)
        self.signer = Signer(cfg.signing_key_hex, cfg.signing_kid) if cfg.signing_key_hex else None
        self.catalog = Catalog(cfg, self.db, self.archives)
        self.stats = Stats(cfg, self.db)
        self.push = PushSender(cfg, self.db)
        self.scheduler = Scheduler(cfg, self)
        self.sources = SourceManager(cfg, self.db, self.catalog)
        self.sources.set_publish_listener(self.scheduler.on_published)
        self.sources.before_pass = self.check_sdk
        self.jobs.on_build_done = self._after_build

        self._push_timer: asyncio.Task | None = None
        self._bootstrap_task: asyncio.Task | None = None
        self.bootstrap_state: dict = {"stage": "idle"}

    async def check_sdk(self) -> None:
        """Reads the SDK index for every channel kept (release and dev) of every
        known target (f7 when none is known). A new SDK is downloaded, deployed
        with its toolchain, and every visible item is queued for it. Runs at
        startup and once a day, so dev moves at most once a day however often
        it is published."""
        for target in self.sdk.targets() or ["f7"]:
            for channel in self.cfg.sdk_channels:
                self.bootstrap_state = {"stage": "sdk index", "target": target, "channel": channel}
                try:
                    profile = await self.sdk.check_index(target, channel)
                except Exception as e:
                    log.warning("sdk index %s/%s failed: %s", target, channel, e)
                    profile = None
                latest = self.sdk.latest(target, channel)
                if latest is not None and not latest.deployed:
                    self.bootstrap_state = {"stage": "sdk deploy", "target": target, "channel": channel, "version": latest.version}
                    log.info("deploying %s sdk %s with its toolchain", channel, latest.version)
                    await self.sdk.ensure_deployed(latest)
                if profile is not None:
                    self.scheduler.on_new_sdk(profile)

    async def bootstrap(self) -> None:
        """What the server does on its own the first time it starts, and
        after every restart that finds something missing: the release SDK of
        every known target (f7 when none is known) is fetched and deployed with
        its toolchain, then each source that was never imported is imported.
        Builds follow from the scheduler as for any new SDK or version."""
        try:
            await self.check_sdk()
            for source in self.sources.sources:
                if source.imported_before:
                    continue
                self.bootstrap_state = {"stage": "import", "source": source.name}
                log.info("bootstrap: first import of %s", source.name)
                report = await self.sources.check_one(source.name)
                log.info("bootstrap: %s imported: %s", source.name, {k: v for k, v in report.items() if k != "errors"})
            self.bootstrap_state = {"stage": "done"}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("bootstrap failed: %s", e)
            self.bootstrap_state = {"stage": "failed", "error": str(e)}

    async def _after_build(self, job) -> None:
        """A ready build may complete a pending push. Flushing is delayed so
        a run of builds (a new SDK, a batch import) folds into one digest."""
        if job is None or job["status"] != "ready" or self.push is None:
            return
        if self._push_timer is not None and not self._push_timer.done():
            self._push_timer.cancel()
        self._push_timer = asyncio.create_task(self._flush_push_later())

    async def _flush_push_later(self) -> None:
        try:
            await asyncio.sleep(self.cfg.push_debounce_sec)
            await self.push.flush()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("push flush failed: %s", e)

    def artifact_file(self, path_text: str | None) -> Path | None:
        if not path_text:
            return None
        path = Path(path_text)
        return path if path.is_file() else None

    def signature_headers(self, path: Path) -> dict:
        if self.signer is None:
            return {}
        return self.signer.headers_for(path)

    def record_download(self, job, client_id: str) -> None:
        if self.stats is None or not job["item_id"]:
            return
        self.stats.record(
            item_id=job["item_id"],
            version_id=job["version_id"] or "",
            family=job["family"],
            target=job["target"],
            api=job["api"],
            client_id=client_id,
        )

    def status(self) -> dict:
        body = {
            "version": __version__,
            "queue_length": self.jobs.queue_length(),
            "sdk": self.sdk.status(),
            "bootstrap": self.bootstrap_state,
        }
        if self.sources is not None:
            body["sources"] = self.sources.status()
        return body


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    state = AppState(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.jobs.start()
        state.scheduler.start()
        state.sources.start()
        if cfg.bootstrap:
            state._bootstrap_task = asyncio.create_task(state.bootstrap())
        yield
        if state._bootstrap_task is not None:
            state._bootstrap_task.cancel()
        if state._push_timer is not None:
            state._push_timer.cancel()
        await state.sources.stop()
        await state.scheduler.stop()
        await state.jobs.stop()
        state.db.close()

    app = FastAPI(
        lifespan=lifespan,
        title="qUnleashed catalog",
        description="Application catalog, source archive and Flibler build service for qUnleashed",
        version=__version__,
        redoc_url=None,
        docs_url="/_docs",
        openapi_url="/_docs/openapi.json",
    )
    app.state.catalog = state


    if cfg.rate_limit_per_min > 0:
        from .auth import client_ip

        windows: dict[str, tuple[int, int]] = {}

        @app.middleware("http")
        async def rate_limit(request: Request, call_next):
            """A fixed one-minute window per client address over /api/."""
            if not request.url.path.startswith("/api/"):
                return await call_next(request)
            now = time.time()
            minute = int(now // 60)
            ip = client_ip(request, cfg)
            current, count = windows.get(ip, (minute, 0))
            if current != minute:
                current, count = minute, 0
                if len(windows) > 50000:
                    windows.clear()
            count += 1
            windows[ip] = (current, count)
            if count > cfg.rate_limit_per_min:
                retry = int(60 - (now % 60)) + 1
                return JSONResponse({"detail": "Too many requests"}, status_code=429, headers={"Retry-After": str(retry)})
            return await call_next(request)

    # Added last so it is the outermost layer: a 429 from the rate limit
    # still carries the CORS headers a browser needs to read it.
    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization", "X-QU-Client", "X-QU-Time", "X-QU-Sign"],
            expose_headers=["X-QU-Sha256", "X-QU-Sign-Ed25519", "X-QU-Kid", "Content-Disposition", "X-Total-Count", "Retry-After"],
            max_age=86400,
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Internal Server Error"}, status_code=500)

    from .routers.admin import build_admin_router
    from .routers.builds import build_router
    from .routers.ci import build_ci_router
    from .routers.v0 import build_v0_router
    from .routers.v1 import build_v1_router

    # Order matters: the v1 family routes end in /{root}, which would otherwise
    # swallow /status, /builds and the CI paths.
    app.include_router(build_router(state))
    app.include_router(build_ci_router(state))
    app.include_router(build_admin_router(state))
    app.include_router(build_v0_router(state))

    @app.get("/api/v1/status")
    async def server_status():
        return state.status()

    app.include_router(build_v1_router(state))

    return app
