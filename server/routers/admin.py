"""Server management, Bearer QU_ADMIN_TOKEN. A wrong token bans the address
for QU_BAN_DAYS, as on the upload endpoint."""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_token_or_ban
from ..jobs import PRIORITY_USER

log = logging.getLogger("catalog.admin")

FAMILY_ROOTS = {"application": "application", "menustyles": "menustyle", "animations": "animation"}


def build_admin_router(state) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
    cfg = state.cfg

    async def admin_dep(request: Request) -> None:
        await require_token_or_ban(request, cfg, state.db, cfg.admin_token, "admin")

    def family_of(root: str) -> str:
        family = FAMILY_ROOTS.get(root)
        if family is None:
            raise HTTPException(status_code=404, detail="Unknown family")
        return family

    # ---------------------------------------------------------------- bans

    @router.get("/bans", dependencies=[Depends(admin_dep)])
    async def bans():
        now = time.time()
        return [
            {"ip": r["ip"], "until": int(r["until"]), "reason": r["reason"]}
            for r in state.db.all("SELECT * FROM bans WHERE until > ? ORDER BY until DESC", (now,))
        ]

    @router.delete("/bans/{ip}", dependencies=[Depends(admin_dep)])
    async def unban(ip: str):
        if state.db.one("SELECT 1 FROM bans WHERE ip = ?", (ip,)) is None:
            raise HTTPException(status_code=404, detail="Not banned")
        state.db.execute("DELETE FROM bans WHERE ip = ?", (ip,))
        return {"status": "unbanned", "ip": ip}

    # --------------------------------------------------------------- items

    def set_hidden(root: str, id_or_alias: str, hidden: bool) -> dict:
        family = family_of(root)
        item = state.catalog.resolve_item(family, id_or_alias)
        if item is None:
            raise HTTPException(status_code=404, detail="Not found")
        state.db.execute("UPDATE items SET hidden = ?, updated_at = ? WHERE id = ?", (int(hidden), time.time(), item["id"]))
        log.info("%s %s by admin", item["id"], "hidden" if hidden else "shown")
        return {"id": item["id"], "hidden": hidden}

    @router.post("/{root}/{id_or_alias}/hide", dependencies=[Depends(admin_dep)])
    async def hide(root: str, id_or_alias: str):
        return set_hidden(root, id_or_alias, True)

    @router.post("/{root}/{id_or_alias}/unhide", dependencies=[Depends(admin_dep)])
    async def unhide(root: str, id_or_alias: str):
        return set_hidden(root, id_or_alias, False)

    # -------------------------------------------------------------- builds

    @router.post("/builds/retry", dependencies=[Depends(admin_dep)])
    async def retry(request: Request):
        """Forgets a failed build and queues it again: one item under one API,
        or under every current SDK when api is left out."""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        if not isinstance(body, dict) or not body.get("id"):
            raise HTTPException(status_code=400, detail="id is required")
        family = family_of(str(body.get("family") or "application"))
        if family == "animation":
            raise HTTPException(status_code=400, detail="Animations are not built")
        item = state.catalog.resolve_item(family, str(body["id"]))
        if item is None or not item["current_version_id"]:
            raise HTTPException(status_code=404, detail="Not found")
        version_id = str(body.get("version_id") or item["current_version_id"])
        if state.catalog.version(version_id) is None:
            raise HTTPException(status_code=404, detail="Version not found")
        target = str(body.get("target") or "f7")
        if body.get("api"):
            profile = state.sdk.profile_for(target, str(body["api"]))
            if profile is None:
                raise HTTPException(status_code=400, detail="No SDK for this api")
            profiles = [profile]
        else:
            profiles = state.sdk.current(target)
        queued = []
        for profile in profiles:
            state.db.execute(
                "DELETE FROM builds WHERE version_id = ? AND target = ? AND sdk_version = ? AND result = 'failed'",
                (version_id, target, profile.version),
            )
            job = state.jobs.submit_scheduled(family, version_id, target, profile.api, PRIORITY_USER,
                                              "retry by admin", force=True, sdk_version=profile.version)
            if job is not None:
                queued.append({"job_id": job["id"], "sdk_version": profile.version, "api": profile.api})
        return {"version_id": version_id, "queued": queued}

    # ----------------------------------------------------------------- sdk

    @router.post("/sdk/check", dependencies=[Depends(admin_dep)])
    async def sdk_check():
        """Reads the SDK index now instead of waiting for the night."""
        await state.check_sdk()
        return {"sdk": state.sdk.status()}

    return router
