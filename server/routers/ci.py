"""What the catalog CI calls, with its Bearer token. Never the app."""

import logging
import shutil
import uuid

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse

from ..auth import ban, client_ip, is_banned, require_ci_token, require_token_or_ban
from ..bundle_tool import BundleError, animation_from_bundle_zip, animation_from_zip, bundle_from_source_zip, manifest_from_bundle_zip
from ..sources.menustyles import INSTALL_PATH as MENUSTYLE_INSTALL_PATH
from ..catalog import PublishError

log = logging.getLogger("catalog.ci")


def build_ci_router(state) -> APIRouter:
    router = APIRouter(tags=["ci"])
    cfg = state.cfg

    async def token_dep(request: Request) -> None:
        await require_ci_token(request, cfg)

    async def save_upload(upload: UploadFile, dest) -> None:
        limit = cfg.max_bundle_mb * 1024 * 1024
        written = 0
        with open(dest, "wb") as out:
            while True:
                chunk = await upload.read(1 << 16)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise HTTPException(status_code=413, detail="Upload is too large")
                out.write(chunk)

    @router.post("/api/v0/application/version/bundle", dependencies=[Depends(token_dep)])
    async def submit_bundle(bundle: UploadFile = File(...)):
        upload_dir = cfg.work_root / "uploads" / uuid.uuid4().hex
        upload_dir.mkdir(parents=True)
        zip_path = upload_dir / "bundle.zip"
        try:
            await save_upload(bundle, zip_path)
            manifest = manifest_from_bundle_zip(zip_path, upload_dir / "bundle", family="application", source="unleashed")
            result = state.catalog.publish(manifest)
        except (BundleError, PublishError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        finally:
            shutil.rmtree(upload_dir, ignore_errors=True)
        if result is None:
            raise HTTPException(status_code=409, detail="Another source owns this appid")
        if state.scheduler is not None:
            state.scheduler.on_published(result, "application")
        return JSONResponse(
            {"id": result.item_id, "version_id": result.version_id, "created": result.created_version},
            status_code=201 if result.created_version else 200,
        )

    @router.post("/api/v1/animations/version/bundle")
    async def submit_animation(request: Request, bundle: UploadFile = File(...)):
        """An animation bundle from the catalog CI. A wrong or missing token
        bans the address for QU_BAN_DAYS, as for /api/v1/sources/upload."""
        await require_token_or_ban(request, cfg, state.db, cfg.ci_token, "animation bundle")
        upload_dir = cfg.work_root / "uploads" / uuid.uuid4().hex
        upload_dir.mkdir(parents=True)
        zip_path = upload_dir / "bundle.zip"
        try:
            await save_upload(bundle, zip_path)
            manifest = animation_from_bundle_zip(zip_path, upload_dir / "work", max_unpack_bytes=cfg.max_unpack_mb * 1024 * 1024)
            result = state.catalog.publish(manifest)
        except (BundleError, PublishError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        finally:
            shutil.rmtree(upload_dir, ignore_errors=True)
        if result is None:
            raise HTTPException(status_code=409, detail="Another source owns this name")
        if state.scheduler is not None:
            state.scheduler.on_published(result, "animation")
        return JSONResponse(
            {"id": result.item_id, "version_id": result.version_id, "created": result.created_version},
            status_code=201 if result.created_version else 200,
        )

    SHA_RE = re.compile(r"^[0-9a-f]{40}$")

    @router.post("/api/v1/sources/upload")
    async def upload_sources(
        request: Request,
        source: UploadFile = File(...),
        family: str = Form("application"),
        origin: str = Form("unleashed"),
        category: str = Form(""),
        name: str = Form(""),
        commit_sha: str = Form(""),
        origin_url: str = Form(""),
    ):
        """A zip of sources from a CI we trust. One unauthorized attempt bans
        the address for QU_BAN_DAYS; the zip is turned into a bundle and
        published like one that came through /application/version/bundle."""
        ip = client_ip(request, cfg)
        if is_banned(state.db, ip):
            raise HTTPException(status_code=403, detail="Banned")
        try:
            await require_ci_token(request, cfg)
        except HTTPException:
            ban(state.db, ip, cfg.ban_days, "unauthorized upload")
            log.warning("upload without a valid token from %s, banned", ip)
            raise HTTPException(status_code=403, detail="Banned")
        if family not in ("application", "menustyle", "animation"):
            raise HTTPException(status_code=400, detail="family must be application, menustyle or animation")
        if origin not in ("unleashed", "atp") or (family != "application" and origin != "unleashed"):
            raise HTTPException(status_code=400, detail="origin must be unleashed, or atp for applications")
        if commit_sha and not SHA_RE.match(commit_sha):
            raise HTTPException(status_code=400, detail="commit_sha must be a full sha")
        upload_dir = cfg.work_root / "uploads" / uuid.uuid4().hex
        upload_dir.mkdir(parents=True)
        zip_path = upload_dir / "source.zip"
        try:
            await save_upload(source, zip_path)
            limit = cfg.max_unpack_mb * 1024 * 1024
            if family == "animation":
                manifest = animation_from_zip(
                    zip_path,
                    upload_dir / "work",
                    name=name,
                    category=category.strip() or None,
                    commit_sha=commit_sha,
                    origin_url=origin_url.strip(),
                    max_unpack_bytes=limit,
                )
            else:
                manifest = bundle_from_source_zip(
                    zip_path,
                    upload_dir / "work",
                    family=family,
                    source=origin,
                    category=category.strip() or (None if family == "application" else "other"),
                    commit_sha=commit_sha,
                    origin_url=origin_url.strip(),
                    max_unpack_bytes=limit,
                )
                if family == "menustyle":
                    manifest.install_path = MENUSTYLE_INSTALL_PATH
            result = state.catalog.publish(manifest)
        except (BundleError, PublishError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        finally:
            shutil.rmtree(upload_dir, ignore_errors=True)
        if result is None:
            raise HTTPException(status_code=409, detail="Another source owns this appid")
        if state.scheduler is not None:
            state.scheduler.on_published(result, family)
        return JSONResponse(
            {"id": result.item_id, "version_id": result.version_id, "created": result.created_version},
            status_code=201 if result.created_version else 200,
        )

    @router.delete("/api/v0/application/{id_or_alias}", dependencies=[Depends(token_dep)])
    async def delete_application(id_or_alias: str):
        item = state.catalog.resolve_item("application", id_or_alias)
        if item is None:
            raise HTTPException(status_code=404, detail="Application not found")
        if not state.catalog.hide("application", item["appid"], "unleashed"):
            raise HTTPException(status_code=409, detail="Another source owns this appid")
        return {"status": "hidden"}

    @router.post("/api/v1/sources/{name}/check", dependencies=[Depends(token_dep)])
    async def check_source(name: str):
        if state.sources is None:
            raise HTTPException(status_code=404, detail="No sources configured")
        return await state.sources.check_one(name)

    return router
