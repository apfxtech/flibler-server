import asyncio
import logging
import re
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import __version__
from .auth import authenticate
from .builder import (
    BundleBuilder,
    BundleDownloadError,
    BundleNotFoundError,
    BundleTimeoutError,
    check_bundle_url,
)
from .cache import BuildCache
from .config import Config
from .jobs import JobManager, JobStatus, SubmitError
from .sdk import CHANNEL_INDEX_IDS, SdkManager

log = logging.getLogger("flibler.api")

TARGET_RE = re.compile(r"^f\d{1,3}$")
API_RE = re.compile(r"^\d{1,5}\.\d{1,5}$")

def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()

    engine_lock = asyncio.Lock()
    cache = BuildCache(cfg.data_dir / "cache", cfg.cache_ttl_days)
    sdk = SdkManager(cfg, engine_lock)
    builder = BundleBuilder(cfg)
    jobs = JobManager(cfg, cache, sdk, builder, engine_lock)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        jobs.start()
        daily = asyncio.create_task(sdk.daily_loop())
        yield
        daily.cancel()
        await jobs.stop()

    app = FastAPI(
        lifespan=lifespan,
        title="ApertureFox flibler",
        description="Flipper asembler for qUnleashed",
        version=__version__,
        redoc_url=None,
        docs_url="/_docs",
        openapi_url="/_docs/openapi.json",
    )
    app.state.cfg = cfg
    app.state.jobs = jobs
    app.state.sdk = sdk

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Internal Server Error"}, status_code=500)

    async def auth_dep(request: Request) -> str:
        return await authenticate(request, cfg)

    def get_job(job_id: str, client_id: str):
        job = jobs.jobs.get(job_id)
        if job is None or job.client_id != client_id:
            raise HTTPException(status_code=404, detail="Build not found")
        return job

    def job_view(job) -> dict:
        view = {
            "id": job.id,
            "status": job.status.value,
            "detail": job.detail,
            "queue_position": jobs.queue_position(job),
            "sdk_version": job.profile.version,
        }
        if job.error:
            view["error"] = job.error
        if job.error_code:
            view["error_code"] = job.error_code
        if job.log_tail:
            view["log_tail"] = job.log_tail
        if job.cache_entry is not None:
            view["fap_name"] = job.cache_entry.meta.get("fap_name")
            view["fap_size"] = job.cache_entry.meta.get("fap_size")
            view["sdk_version"] = job.cache_entry.sdk_version
        return view

    def validate_request(body: dict) -> dict:
        bundle_url = body.get("bundle_url", "")
        parsed = urlparse(bundle_url)
        if parsed.scheme != "https" or parsed.hostname not in cfg.allowed_bundle_hosts:
            raise HTTPException(status_code=400, detail="Invalid bundle_url")

        target = body.get("target", "")
        if not TARGET_RE.match(target):
            raise HTTPException(status_code=400, detail="Invalid target")

        api = str(body.get("api", "") or "")
        if api and not API_RE.match(api):
            raise HTTPException(status_code=400, detail="Invalid api")

        channel = body.get("channel") or "release"
        if channel not in CHANNEL_INDEX_IDS:
            raise HTTPException(status_code=400, detail="Invalid channel")

        index_url = body.get("index_url") or cfg.default_index_url
        if index_url not in cfg.allowed_index_urls:
            raise HTTPException(status_code=400, detail="Index URL is not allowed")

        def clean(name: str, limit: int = 128) -> str:
            value = str(body.get(name, "") or "")
            return value[:limit]

        return {
            "bundle_url": bundle_url,
            "target": target,
            "api": api,
            "channel": channel,
            "index_url": index_url,
            "alias": clean("alias"),
            "uid": clean("uid"),
            "version_uid": clean("version_uid"),
        }

    @app.post("/api/v1/builds")
    async def submit_build(request: Request, client_id: str = Depends(auth_dep)):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON")
        normalized = validate_request(body)
        try:
            await asyncio.to_thread(check_bundle_url, normalized["bundle_url"])
        except BundleNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except BundleTimeoutError as e:
            raise HTTPException(status_code=504, detail=str(e))
        except BundleDownloadError as e:
            raise HTTPException(status_code=502, detail=str(e))
        try:
            job, created = jobs.submit(normalized, client_id)
        except SubmitError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)
        view = job_view(job)
        view["cached"] = job.status == JobStatus.READY and not created
        return JSONResponse(view, status_code=201 if created else 200)

    @app.get("/api/v1/builds/{job_id}")
    async def build_status(job_id: str, client_id: str = Depends(auth_dep)):
        return job_view(get_job(job_id, client_id))

    @app.get("/api/v1/builds/{job_id}/artifact")
    async def build_artifact(job_id: str, client_id: str = Depends(auth_dep)):
        job = get_job(job_id, client_id)
        if job.status == JobStatus.FAILED:
            raise HTTPException(status_code=409, detail=job.error or "Build failed")
        if job.status != JobStatus.READY or job.cache_entry is None:
            raise HTTPException(status_code=409, detail="Build is not ready yet")
        entry = job.cache_entry
        if not entry.fap_path.is_file():
            raise HTTPException(status_code=410, detail="Artifact expired")
        return FileResponse(
            entry.fap_path,
            media_type="application/octet-stream",
            filename=entry.meta.get("fap_name", "app.fap"),
        )

    @app.delete("/api/v1/builds/{job_id}")
    async def cancel_build(job_id: str, client_id: str = Depends(auth_dep)):
        job = get_job(job_id, client_id)
        if not jobs.cancel(job):
            raise HTTPException(
                status_code=409, detail="Only queued builds can be cancelled"
            )
        return {"status": "cancelled"}

    @app.get("/api/v1/status")
    async def server_status():
        return {
            "version": __version__,
            "queue_length": len(jobs.queue),
            "sdk": sdk.status(),
        }

    return app
