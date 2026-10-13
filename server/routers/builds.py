import asyncio
import json
import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import authenticate
from .. import builder as builder_module
from ..builder import BundleDownloadError, BundleNotFoundError, BundleTimeoutError
from ..jobs import STATUS_FAILED, STATUS_READY, SubmitError

TARGET_RE = re.compile(r"^f\d{1,3}$")
API_RE = re.compile(r"^\d{1,5}\.\d{1,5}$")
ID_RE = re.compile(r"^[A-Za-z0-9:@_.-]{1,160}$")


def build_router(state) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["builds"])
    cfg = state.cfg
    jobs = state.jobs

    async def auth_dep(request: Request) -> str:
        return await authenticate(request, cfg)

    def get_job(job_id: str, client_id: str):
        row = jobs.job(job_id)
        if row is None or not jobs.is_subscriber(job_id, client_id):
            raise HTTPException(status_code=404, detail="Build not found")
        return row

    def validate(body: dict) -> dict:
        target = str(body.get("target", "") or "")
        if not TARGET_RE.match(target):
            raise HTTPException(status_code=400, detail="Invalid target")
        api = str(body.get("api", "") or "")
        if api and not API_RE.match(api):
            raise HTTPException(status_code=400, detail="Invalid api")
        family = str(body.get("family", "") or "application")
        if family not in ("application", "menustyle"):
            raise HTTPException(status_code=400, detail="Invalid family")
        version_id = str(body.get("version_id", "") or "")
        if version_id and not ID_RE.match(version_id):
            raise HTTPException(status_code=400, detail="Invalid version_id")
        bundle_url = str(body.get("bundle_url", "") or "")
        if bundle_url:
            parsed = urlparse(bundle_url)
            if parsed.scheme != "https" or parsed.hostname not in cfg.bundle_hosts():
                raise HTTPException(status_code=400, detail="Invalid bundle_url")
        if not version_id and not bundle_url:
            raise HTTPException(status_code=400, detail="version_id or bundle_url is required")
        appid = str(body.get("alias", "") or body.get("appid", "") or "")[:128]
        return {
            "target": target,
            "api": api,
            "family": family,
            "version_id": version_id,
            "bundle_url": bundle_url,
            "appid": appid,
        }

    @router.post("/builds")
    async def submit_build(request: Request, client_id: str = Depends(auth_dep)):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON")
        normalized = validate(body)
        if normalized["bundle_url"] and not normalized["version_id"]:
            try:
                await asyncio.to_thread(builder_module.check_bundle_url, normalized["bundle_url"], cfg.bundle_hosts())
            except BundleNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e))
            except BundleTimeoutError as e:
                raise HTTPException(status_code=504, detail=str(e))
            except BundleDownloadError as e:
                raise HTTPException(status_code=502, detail=str(e))
        try:
            job, created = jobs.submit_user(normalized, client_id)
        except SubmitError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)
        view = jobs.job_view(job)
        view["cached"] = job["status"] == STATUS_READY and not created
        return JSONResponse(view, status_code=201 if created else 200)

    @router.get("/builds/{job_id}")
    async def build_status(job_id: str, client_id: str = Depends(auth_dep)):
        return jobs.job_view(get_job(job_id, client_id))

    @router.get("/builds/{job_id}/events")
    async def build_events(job_id: str, client_id: str = Depends(auth_dep)):
        get_job(job_id, client_id)

        async def stream():
            counter = 0
            async for view in jobs.events(job_id):
                if view is None:
                    yield ": keepalive\n\n"
                    continue
                counter += 1
                name = view["status"]
                if name not in (STATUS_READY, STATUS_FAILED):
                    name = "sdk" if view.get("detail", "").startswith("deploying") else view["status"]
                payload = json.dumps(view, ensure_ascii=False)
                yield f"id: {counter}\nevent: {name}\ndata: {payload}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/builds/{job_id}/artifact")
    async def build_artifact(job_id: str, request: Request, client_id: str = Depends(auth_dep)):
        job = get_job(job_id, client_id)
        if job["status"] == STATUS_FAILED:
            raise HTTPException(status_code=409, detail=job["error"] or "Build failed")
        if job["status"] != STATUS_READY or not job["artifact_path"]:
            raise HTTPException(status_code=409, detail="Build is not ready yet")
        path = state.artifact_file(job["artifact_path"])
        if path is None:
            raise HTTPException(status_code=410, detail="Artifact expired")
        state.record_download(job, client_id)
        return state.artifact_response(request, path, job["artifact_name"] or path.name, None)

    @router.delete("/builds/{job_id}")
    async def cancel_build(job_id: str, client_id: str = Depends(auth_dep)):
        get_job(job_id, client_id)
        jobs.cancel(job_id, client_id)
        return {"status": "cancelled"}

    return router
