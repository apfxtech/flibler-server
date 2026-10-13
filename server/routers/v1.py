"""API v1: the three families, authors, the new feed, tops and what is broken.
Public like v0; builds are never created here."""

import hashlib
import json
import re

import yaml
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from ..db import loads
from ..query import ListQuery
from ..stats import PERIODS
from ..views import Presenter

TARGET_RE = re.compile(r"^f\d{1,3}$")
API_RE = re.compile(r"^\d{1,5}\.\d{1,5}$")
FAMILY_ROOTS = {"application": "application", "menustyle": "menustyles", "animation": "animations"}
ORIGINS = ("ofw", "unleashed", "atp")

DOLPHIN_DEFAULTS = {"min_butthurt": 0, "max_butthurt": 14, "min_level": 1, "max_level": 30, "weight": 8}


def _opt(value: str | None, pattern: re.Pattern) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if not pattern.match(value):
        raise HTTPException(status_code=400, detail="invalid value")
    return value


def _int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def build_v1_router(state) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["v1"])
    present = Presenter(state)
    listing = ListQuery(state)
    catalog = state.catalog

    def client_hint(request: Request) -> str:
        return request.headers.get("x-qu-client") or (request.client.host if request.client else "")

    def family_of(root: str) -> str:
        for family, name in FAMILY_ROOTS.items():
            if name == root:
                return family
        raise HTTPException(status_code=404, detail="Unknown family")

    def listing_params(params: dict, family: str) -> dict:
        target, api = _opt(params.get("target"), TARGET_RE), _opt(params.get("api"), API_RE)
        if family == "animation":
            target = api = None
        origins = params.get("origin")
        if isinstance(origins, str):
            origins = [o for o in origins.split(",") if o]
        if origins and any(o not in ORIGINS for o in origins):
            raise HTTPException(status_code=400, detail="unknown origin")
        return {
            "target": target,
            "api": api,
            "origins": origins or None,
            "category_id": params.get("category_id") or None,
            "query": params.get("query") or None,
            "author_id": params.get("author_id") or None,
            "sort_by": str(params.get("sort_by") or "updated_at"),
            "sort_order": _int(params.get("sort_order"), -1, -1, 1) or -1,
            "limit": _int(params.get("limit"), 48, 1, 500),
            "offset": _int(params.get("offset"), 0, 0, 10**7),
        }

    # -------------------------------------------------------------- common

    @router.get("/feed/new")
    async def feed_new(request: Request, since: float | None = None, family: str | None = None, origin: str | None = None, limit: int = 10):
        """The `limit` newest of each family, however old; `since` narrows it to what changed after that time."""
        families = [family] if family in FAMILY_ROOTS else list(FAMILY_ROOTS)
        limit = max(1, min(limit, 100))
        base = present.base_url(request)
        out = []
        for fam in families:
            rows = listing.run(family=fam, origins=[origin] if origin else None, since=since, sort_by="updated_at", limit=limit, unfiltered=True)
            out.extend(present.card(row, base, v1=True) for row in rows)
        out.sort(key=lambda c: c["updated_at"], reverse=True)
        return out

    @router.get("/stats/top")
    async def stats_top(request: Request, family: str | None = None, origin: str | None = None, period: str = "week", limit: int = 3):
        if period not in PERIODS:
            raise HTTPException(status_code=400, detail="period must be week, month, season or year")
        if state.stats is None:
            return []
        base = present.base_url(request)
        out = []
        for item_id, count in state.stats.top(family, origin, period, _int(limit, 3, 1, 50)):
            item = catalog.item(item_id)
            if item is not None and not item["hidden"]:
                card = present.card(item, base, v1=True)
                card["downloads_in_period"] = count
                out.append(card)
        return out

    def author_table(query: str | None) -> list[dict]:
        rows = state.db.all("SELECT id, family, authors FROM items WHERE hidden = 0")
        counts = state.stats.counts("week") if state.stats else {}
        table: dict[str, dict] = {}
        for row in rows:
            for author in loads(row["authors"], []):
                entry = table.setdefault(author["id"], {"id": author["id"], "name": author["name"], "counts": {}, "downloads_week": 0})
                entry["counts"][row["family"]] = entry["counts"].get(row["family"], 0) + 1
                entry["downloads_week"] += counts.get(row["id"], 0)
        out = list(table.values())
        if query:
            q = query.lower()
            out = [a for a in out if q in a["id"] or q in a["name"].lower()]
        return out

    @router.get("/search")
    async def search(request: Request, query: str, target: str | None = None, api: str | None = None,
                     family: str | None = None, limit: int = 20):
        """One query across the three families, and the authors whose @name
        matches it. Each family is filtered as its own list would be."""
        query = query.strip()
        if not query:
            raise HTTPException(status_code=400, detail="query is required")
        target, api = _opt(target, TARGET_RE), _opt(api, API_RE)
        families = [family] if family in FAMILY_ROOTS else list(FAMILY_ROOTS)
        base = present.base_url(request)
        per = _int(limit, 20, 1, 100)
        items = []
        totals = {}
        for fam in families:
            fam_target, fam_api = (None, None) if fam == "animation" else (target, api)
            rows = listing.run(family=fam, target=fam_target, api=fam_api, query=query, sort_by="downloads_month", limit=per)
            totals[fam] = listing.count(family=fam, target=fam_target, api=fam_api, query=query)
            items.extend(present.card(row, base, fam_target, fam_api, v1=True) for row in rows)
        authors = sorted(author_table(query), key=lambda a: -a["downloads_week"])[:per]
        return {"items": items, "totals": totals, "authors": authors}

    @router.get("/authors")
    async def authors(query: str | None = None, sort_by: str = "name", limit: int = 50, offset: int = 0):
        out = author_table(query)
        if sort_by == "items":
            out.sort(key=lambda a: -sum(a["counts"].values()))
        elif sort_by == "downloads_week":
            out.sort(key=lambda a: -a["downloads_week"])
        else:
            out.sort(key=lambda a: a["id"])
        return out[offset : offset + _int(limit, 50, 1, 500)]

    @router.get("/authors/{author_id}")
    async def author(author_id: str):
        rows = state.db.all("SELECT id, family, name, authors FROM items WHERE hidden = 0")
        counts = state.stats.counts(None) if state.stats else {}
        found = None
        per_family: dict[str, int] = {}
        downloads = 0
        for row in rows:
            for a in loads(row["authors"], []):
                if a["id"] == author_id.lower():
                    found = found or a
                    per_family[row["family"]] = per_family.get(row["family"], 0) + 1
                    downloads += counts.get(row["id"], 0)
        if found is None:
            raise HTTPException(status_code=404, detail="Author not found")
        return {"id": found["id"], "name": found["name"], "counts": per_family, "downloads": downloads}

    @router.get("/broken")
    async def broken(family: str | None = None, api: str | None = None, origin: str | None = None, limit: int = 200):
        clauses = ["b.result = 'failed'", "i.hidden = 0", "i.current_version_id = b.version_id"]
        params: list = []
        if family:
            clauses.append("i.family = ?")
            params.append(family)
        if api:
            clauses.append("b.api = ?")
            params.append(api)
        if origin:
            clauses.append("i.origin = ?")
            params.append(origin)
        rows = state.db.all(
            "SELECT i.id, i.alias, i.family, i.origin, v.version, b.target, b.api, b.sdk_version, b.error, b.built_at, "
            "(SELECT api FROM builds o WHERE o.version_id = b.version_id AND o.target = b.target AND o.result = 'ok' ORDER BY built_at DESC LIMIT 1) AS last_ok_api "
            f"FROM builds b JOIN versions v ON v.id = b.version_id JOIN items i ON i.id = v.item_id WHERE {' AND '.join(clauses)} "
            "ORDER BY b.built_at DESC LIMIT ?",
            (*params, _int(limit, 200, 1, 1000)),
        )
        return [
            {"item": r["id"], "alias": r["alias"], "family": r["family"], "origin": r["origin"], "version": r["version"],
             "target": r["target"], "api": r["api"], "sdk_version": r["sdk_version"], "last_ok_api": r["last_ok_api"],
             "error": r["error"], "since": int(r["built_at"]),
             "log_url": f"/api/v1/broken/{r['id']}/log?target={r['target']}&api={r['api']}"}
            for r in rows
        ]

    @router.get("/broken/{item_id}/log")
    async def broken_log(item_id: str, target: str, api: str):
        item = catalog.item(item_id)
        if item is None or not item["current_version_id"]:
            raise HTTPException(status_code=404, detail="Not found")
        row = state.db.one(
            "SELECT log_tail FROM builds WHERE version_id = ? AND target = ? AND api = ? AND result = 'failed' ORDER BY built_at DESC LIMIT 1",
            (item["current_version_id"], target, api),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="No failed build")
        return Response(content=row["log_tail"] or "", media_type="text/plain")

    @router.get("/signing")
    async def signing():
        if state.signer is None:
            return {"enabled": False}
        return {"enabled": True, **state.signer.describe()}

    # ------------------------------------------------------------ families

    @router.get("/{root}/category")
    async def categories(root: str, request: Request, target: str | None = None, api: str | None = None):
        family = family_of(root)
        target, api = (None, None) if family == "animation" else (_opt(target, TARGET_RE), _opt(api, API_RE))
        base = present.base_url(request)
        counts = listing.count_by_category(family, target, api)
        rows = state.db.all("SELECT * FROM categories WHERE family = ? ORDER BY parent_id IS NOT NULL, priority, name", (family,))
        return [present.category_json(row, base, counts.get(row["id"], 0)) for row in rows]

    @router.get("/{root}")
    async def list_items(root: str, request: Request, response: Response):
        family = family_of(root)
        params = listing_params(dict(request.query_params), family)
        rows = listing.run(family=family, **params)
        response.headers["X-Total-Count"] = str(listing.count(family=family, **params))
        base = present.base_url(request)
        return [present.card(row, base, params["target"], params["api"], v1=True) for row in rows]

    @router.post("/{root}/lookup")
    async def lookup(root: str, request: Request):
        family = family_of(root)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not ids:
            raise HTTPException(status_code=400, detail="ids is required")
        params = listing_params(body, family)
        rows = listing.run(family=family, ids=[str(i) for i in ids[:500]], limit=500, unfiltered=True,
                           target=params["target"], api=params["api"])
        base = present.base_url(request)
        return [present.card(row, base, params["target"], params["api"], v1=True) for row in rows]

    @router.get("/{root}/version/{version_id}/build/compatible")
    async def compatible(root: str, version_id: str, request: Request, target: str, api: str):
        family = family_of(root)
        if family == "animation":
            raise HTTPException(status_code=404, detail="Animations have no builds; use plan")
        target, api = _opt(target, TARGET_RE), _opt(api, API_RE)
        version = catalog.version(version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="Version not found")
        ready = state.jobs.ready_build(version_id, target, api)
        if ready is None:
            status = state.jobs.build_state(version_id, target, api)
            raise HTTPException(status_code=409 if status == "failed" else 404, detail={"status": status})
        item = catalog.item(version["item_id"])
        state.sdk.touch(target, api)
        if state.stats is not None and item is not None:
            state.stats.record(item_id=item["id"], version_id=version_id, family=family, target=target, api=api, client_id=client_hint(request))
        path = state.artifact_file(ready["artifact_path"])
        return FileResponse(path, media_type="application/octet-stream", filename=ready["artifact_name"], headers=state.signature_headers(path))

    @router.get("/{root}/version/{version_id}/bundle")
    async def bundle(root: str, version_id: str):
        family_of(root)
        version = catalog.version(version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="Version not found")
        path = catalog.bundle_zip(version)
        if path is None:
            raise HTTPException(status_code=404, detail="This version has no sources")
        return FileResponse(path, media_type="application/zip", filename="bundle.zip", headers=state.signature_headers(path))

    @router.get("/{root}/version/{version_id}/plan")
    async def plan(root: str, version_id: str, request: Request):
        family = family_of(root)
        version = catalog.version(version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="Version not found")
        item = catalog.item(version["item_id"])
        base = present.base_url(request)
        if family != "animation":
            raise HTTPException(status_code=404, detail="Only animations have a plan; use build/compatible")
        archive = state.archives[family]
        files = []
        entry = {"name": item["appid"], **DOLPHIN_DEFAULTS}
        for name in archive.list_files(version["tag"], version["archive_path"]):
            data = archive.read_file(version["tag"], f"{version['archive_path']}/{name}")
            if name == "manifest.yml":
                try:
                    catalog_manifest = yaml.safe_load(data) or {}
                except yaml.YAMLError:
                    catalog_manifest = {}
                if isinstance(catalog_manifest, dict):
                    entry.update({k: catalog_manifest[k] for k in DOLPHIN_DEFAULTS if isinstance(catalog_manifest.get(k), int)})
                continue
            files.append({
                "device_path": f"{version['install_path']}/{name}",
                "uri": f"{base}/api/v1/{root}/version/{version_id}/file/{name}",
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
        if state.stats is not None and item is not None:
            state.stats.record(item_id=item["id"], version_id=version_id, family=family, target="", api="", client_id=client_hint(request))
        return {
            "files": files,
            "manifest_merge": {
                "path": "/ext/dolphin/manifest.txt",
                "format": "dolphin",
                "entry": entry,
            },
        }

    @router.get("/{root}/version/{version_id}/file/{name:path}")
    async def plan_file(root: str, version_id: str, name: str):
        family = family_of(root)
        version = catalog.version(version_id)
        if version is None or "/" in name.strip("/") and ".." in name:
            raise HTTPException(status_code=404, detail="Not found")
        if ".." in name.split("/"):
            raise HTTPException(status_code=404, detail="Not found")
        try:
            data = state.archives[family].read_file(version["tag"], f"{version['archive_path']}/{name}")
        except Exception:
            raise HTTPException(status_code=404, detail="File not found")
        headers = {}
        if state.signer is not None:
            sha = hashlib.sha256(data).hexdigest()
            headers = {"X-QU-Sha256": sha, "X-QU-Sign-Ed25519": state.signer.sign_digest(sha), "X-QU-Kid": state.signer.kid}
        return Response(content=data, media_type="application/octet-stream", headers=headers)

    @router.get("/{root}/{id_or_alias}/versions")
    async def versions(root: str, id_or_alias: str, request: Request, target: str | None = None, api: str | None = None):
        family = family_of(root)
        target, api = _opt(target, TARGET_RE), _opt(api, API_RE)
        item = catalog.resolve_item(family, id_or_alias)
        if item is None:
            raise HTTPException(status_code=404, detail="Not found")
        base = present.base_url(request)
        out = []
        for version in catalog.versions_of(item["id"]):
            entry = present.version_json(item, version, base, target, api, False)
            entry["created_at"] = int(version["created_at"])
            entry["commit_sha"] = version["commit_sha"]
            if family != "animation" and target and api:
                entry["availability"] = present.availability(version["id"], target, api)
            out.append(entry)
        return out

    @router.get("/{root}/{id_or_alias}")
    async def detail(root: str, id_or_alias: str, request: Request, target: str | None = None, api: str | None = None):
        family = family_of(root)
        target, api = (None, None) if family == "animation" else (_opt(target, TARGET_RE), _opt(api, API_RE))
        item = catalog.resolve_item(family, id_or_alias)
        if item is None or item["hidden"]:
            raise HTTPException(status_code=404, detail="Not found")
        return present.card(item, present.base_url(request), target, api, detailed=True, v1=True)

    return router
