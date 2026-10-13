"""Archivarius API v0, the contract the OFW mobile apps and qUnleashed read."""

import logging
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from ..query import ListQuery
from ..views import Presenter

log = logging.getLogger("catalog.v0")

TARGET_RE = re.compile(r"^f\d{1,3}$")
API_RE = re.compile(r"^\d{1,5}\.\d{1,5}$")
SORT_MAP = {"updated_at": "updated_at", "created_at": "created_at", "name": "name"}


def _opt(value: str | None, pattern: re.Pattern) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if not pattern.match(value):
        raise HTTPException(status_code=400, detail="is not exists")
    return value


def _int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def build_v0_router(state) -> APIRouter:
    router = APIRouter(prefix="/api/v0", tags=["v0"])
    present = Presenter(state)
    listing = ListQuery(state)
    catalog = state.catalog

    def check_sdk(target: str | None, api: str | None) -> None:
        """OFW answers 400 'is not exists' for an api it has never seen;
        the client relies on that to drop its filters (ADR 0005 feed logic)."""
        if api and target and api not in state.sdk.known_apis(target):
            raise HTTPException(status_code=400, detail=f"SDK {api} for {target} is not exists")

    def client_hint(request: Request) -> str:
        return request.headers.get("x-qu-client") or (request.client.host if request.client else "")

    # ----------------------------------------------------------------- sdk

    @router.get("/sdk")
    async def sdk_list():
        return present.sdk_list_json()

    # ------------------------------------------------------------ category

    def ofw_422(loc: list, msg: str, type_: str, ctx: dict | None = None) -> JSONResponse:
        entry = {"loc": loc, "msg": msg, "type": type_}
        if ctx is not None:
            entry["ctx"] = ctx
        return JSONResponse({"detail": [entry], "body": None}, status_code=422)

    @router.get("/category")
    async def categories(request: Request):
        """As Archivarius: counts are filtered only when both target and api
        are given and the api is one the catalogue has; the target value
        itself does not change the count; anything else counts unfiltered."""
        params = request.query_params
        raw_limit = params.get("limit")
        limit = None
        if raw_limit is not None:
            try:
                limit = int(raw_limit)
            except ValueError:
                return ofw_422(["query", "limit"], "value is not a valid integer", "type_error.integer")
            if limit < 0:
                return ofw_422(["query", "limit"], "ensure this value is greater than or equal to 0",
                               "value_error.number.not_ge", {"limit_value": 0})
        target = (params.get("target") or "").strip()
        api = (params.get("api") or "").strip()
        known = {row["api"] for row in state.db.all("SELECT DISTINCT api FROM sdks")}
        counted_api = api if target and api and api in known else None
        base = present.base_url(request)
        counts = listing.count_by_category("application", None, counted_api)
        rows = state.db.all("SELECT * FROM categories WHERE family = 'application' AND parent_id IS NULL ORDER BY priority")
        if limit is not None:
            rows = rows[:limit]
        return [present.category_json_v0(row, base, counts.get(row["id"], 0)) for row in rows]

    OBJECT_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")

    @router.get("/category/{uid}/icon")
    async def category_icon(uid: str):
        if not OBJECT_ID_RE.match(uid):
            return ofw_422(["path", "uid"], "ObjectId string is invalid!", "type_error")
        row = state.db.one("SELECT * FROM categories WHERE family = 'application' AND id = ?", (uid,))
        path = present.category_icon_file(row) if row is not None else None
        if path is None:
            return JSONResponse(
                {"detail": {"status": "error", "code": 1004, "details": f"Category `{uid}` not found!"}},
                status_code=404,
            )
        return Response(content=path.read_bytes(), media_type="image/svg+xml")

    # --------------------------------------------------------- application

    def list_apps(request: Request, params: dict) -> list[dict]:
        target, api = _opt(params.get("target"), TARGET_RE), _opt(params.get("api"), API_RE)
        check_sdk(target, api)
        sort_by = SORT_MAP.get(str(params.get("sort_by") or "updated_at"), "updated_at")
        sort_order = _int(params.get("sort_order"), -1, -1, 1) or -1
        ids = params.get("applications")
        if ids is not None and not isinstance(ids, list):
            ids = None
        rows = listing.run(
            family="application",
            target=target,
            api=api,
            category_id=(params.get("category_id") or None),
            query=(params.get("query") or None),
            ids=[str(i) for i in ids] if ids else None,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=_int(params.get("limit"), 48, 1, 500),
            offset=_int(params.get("offset"), 0, 0, 10**7),
        )
        base = present.base_url(request)
        return [present.card(row, base, target, api) for row in rows]

    @router.get("/0/application")
    async def applications_get(request: Request):
        return list_apps(request, dict(request.query_params))

    @router.post("/1/application")
    async def applications_post(request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON")
        return list_apps(request, body)

    @router.get("/0/application/featured")
    async def featured(request: Request, limit: int = 50, offset: int = 0):
        base = present.base_url(request)
        top = state.stats.top("application", None, "week", limit + offset) if state.stats else []
        out = []
        for item_id, _ in top[offset:]:
            item = catalog.item(item_id)
            if item is not None and not item["hidden"]:
                out.append(present.card(item, base))
        return out

    @router.get("/application/{id_or_alias}")
    async def application(id_or_alias: str, request: Request, api: str | None = None, target: str | None = None):
        target, api = _opt(target, TARGET_RE), _opt(api, API_RE)
        check_sdk(target, api)
        item = catalog.resolve_item("application", id_or_alias)
        if item is None or item["hidden"]:
            raise HTTPException(status_code=404, detail="Application not found")
        return present.card(item, present.base_url(request), target, api, detailed=True)

    @router.get("/application/{id_or_alias}/versions")
    async def application_versions(id_or_alias: str, request: Request, version: str | None = None):
        item = catalog.resolve_item("application", id_or_alias)
        if item is None:
            raise HTTPException(status_code=404, detail="Application not found")
        base = present.base_url(request)
        rows = catalog.versions_of(item["id"])
        if version is not None:
            rows = [r for r in rows if r["version"] == version]
            if not rows:
                raise HTTPException(status_code=404, detail="Version not found")
        return [present.version_json(item, row, base, None, None, False) for row in rows]

    @router.post("/1/application/versions")
    async def versions_batch(request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        ids = body.get("application_versions") if isinstance(body, dict) else None
        if not isinstance(ids, list):
            raise HTTPException(status_code=400, detail="application_versions is required")
        base = present.base_url(request)
        out = []
        for version_id in ids[:500]:
            version = catalog.version(str(version_id))
            if version is None:
                continue
            item = catalog.item(version["item_id"])
            if item is not None:
                out.append(present.version_json(item, version, base, None, None, False))
        return out

    @router.post("/application/{uid}/issue")
    async def report_issue(uid: str, request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        log.warning("issue reported for %s: %s", uid, str(body)[:500])
        return JSONResponse({"status": "accepted"}, status_code=202)

    # ---------------------------------------------------------- artifacts

    @router.get("/application/version/{version_id}/build/compatible")
    async def compatible_build(version_id: str, request: Request, target: str, api: str):
        target, api = _opt(target, TARGET_RE), _opt(api, API_RE)
        if not target or not api:
            raise HTTPException(status_code=400, detail="target and api are required")
        version = catalog.version(version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="Version not found")
        item = catalog.item(version["item_id"])
        ready = state.jobs.ready_build(version_id, target, api)
        if ready is None:
            status = state.jobs.build_state(version_id, target, api)
            if status == "failed":
                raise HTTPException(status_code=409, detail={"status": "failed"})
            raise HTTPException(status_code=404, detail={"status": status})
        state.sdk.touch(target, api)
        if state.stats is not None and item is not None:
            state.stats.record(item_id=item["id"], version_id=version_id, family=item["family"], target=target, api=api, client_id=client_hint(request))
        path = state.artifact_file(ready["artifact_path"])
        return state.artifact_response(request, path, ready["artifact_name"], ready["sha256"])

    @router.get("/application/version/{version_id}/bundle")
    async def bundle(version_id: str):
        version = catalog.version(version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="Version not found")
        path = catalog.bundle_zip(version)
        if path is None:
            raise HTTPException(status_code=404, detail="This version has no sources")
        return FileResponse(path, media_type="application/zip", filename="bundle.zip", headers=state.signature_headers(path))

    @router.get("/application/version/assets/{asset_id}")
    async def asset(asset_id: str):
        row = catalog.asset(asset_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        return FileResponse(row["path"], media_type=row["content_type"], headers={"Cache-Control": "public, max-age=31536000, immutable"})

    return router
