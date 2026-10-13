"""JSON shapes. v0 is the Archivarius contract both clients read; v1 adds
fields on top of the same card and never removes any."""

import time
from pathlib import Path

from .db import loads

CATEGORY_ICONS = Path(__file__).parent / "assets" / "category_icons"

STATUS_READY = "READY"
STATUS_RUNNING = "BUILD_RUNNING"
STATUS_UNSUPPORTED = "UNSUPPORTED_SDK"


class Presenter:
    def __init__(self, state):
        self.state = state
        self.cfg = state.cfg
        self.db = state.db

    # ------------------------------------------------------------------- urls

    def base_url(self, request=None) -> str:
        if self.cfg.public_base_url:
            return self.cfg.public_base_url
        if request is not None:
            return str(request.base_url).rstrip("/")
        return ""

    def asset_url(self, asset_id: str | None, base: str) -> str:
        return f"{base}/api/v0/application/version/assets/{asset_id}" if asset_id else ""

    # ------------------------------------------------------------------ builds

    def latest_ok_build(self, version_id: str, target: str | None, api: str | None):
        clauses = ["version_id = ?", "result = 'ok'"]
        params: list = [version_id]
        if target:
            clauses.append("target = ?")
            params.append(target)
        if api:
            clauses.append("api = ?")
            params.append(api)
        return self.db.one(
            f"SELECT * FROM builds WHERE {' AND '.join(clauses)} ORDER BY built_at DESC LIMIT 1", tuple(params)
        )

    def sdk_json(self, build) -> dict | None:
        if build is None:
            return None
        latest = self.state.sdk.latest(build["target"])
        return {
            "id": build["sdk_version"],
            "name": build["sdk_version"],
            "target": build["target"],
            "api": build["api"],
            "is_latest_release": bool(latest and latest.version == build["sdk_version"]),
        }

    def build_json(self, build) -> dict | None:
        if build is None:
            return None
        return {
            "id": f"{build['version_id']}:{build['target']}:{build['sdk_version']}",
            "_id": f"{build['version_id']}:{build['target']}:{build['sdk_version']}",
            "sdk": self.sdk_json(build),
            "metadata": {"length": build["size"] or 0},
        }

    def version_status(self, family: str, version_id: str, target: str | None, api: str | None) -> str:
        if family == "animation":
            return STATUS_READY
        if not target or not api:
            return STATUS_READY if self.latest_ok_build(version_id, None, None) else STATUS_UNSUPPORTED
        state = self.state.jobs.build_state(version_id, target, api)
        if state in ("ready", "expired"):
            return STATUS_READY
        active = self.db.one(
            "SELECT 1 FROM jobs WHERE version_id = ? AND target = ? AND api = ? AND status IN ('queued', 'building')",
            (version_id, target, api),
        )
        return STATUS_RUNNING if active else STATUS_UNSUPPORTED

    def availability(self, version_id: str, target: str, api: str) -> dict:
        state = self.state.jobs.build_state(version_id, target, api)
        last_ok = self.db.one(
            "SELECT api, built_at FROM builds WHERE version_id = ? AND target = ? AND result = 'ok' ORDER BY built_at DESC LIMIT 1",
            (version_id, target),
        )
        failed = self.db.one(
            "SELECT built_at FROM builds WHERE version_id = ? AND target = ? AND api = ? AND result = 'failed' ORDER BY built_at DESC LIMIT 1",
            (version_id, target, api),
        )
        return {
            "status": state,
            "last_ok_api": last_ok["api"] if last_ok else None,
            "failed_at": failed["built_at"] if failed and state == "failed" else None,
        }

    # ------------------------------------------------------------------- cards

    def version_json(self, item, version, base: str, target: str | None, api: str | None, detailed: bool) -> dict:
        build = self.latest_ok_build(version["id"], target, api) if item["family"] != "animation" else None
        out = {
            "_id": version["id"],
            "id": version["id"],
            "name": version["name"] or item["name"],
            "version": version["version"],
            "short_description": version["short_description"] or item["short_description"],
            "icon_uri": self.asset_url(version["icon_asset"], base),
            "screenshots": [self.asset_url(a, base) for a in loads(version["screenshots"], [])],
            "status": self.version_status(item["family"], version["id"], target, api),
            "current_build": self.build_json(build),
            "application_id": item["id"],
            "bundle_id": version["id"],
        }
        if detailed:
            links = loads(item["links"], {})
            root = self.family_root(item["family"], base)
            out["description"] = version["description"]
            out["changelog"] = version["changelog"]
            out["links"] = {
                "bundle_uri": f"{root}/version/{version['id']}/bundle" if version["has_sources"] else None,
                "manifest_uri": links.get("manifest_uri"),
                "source_code": links.get("source_code") or ({"uri": ""}),
            }
        return out

    def family_root(self, family: str, base: str) -> str:
        if family == "application":
            return f"{base}/api/v0/application"
        return f"{base}/api/v1/{'menustyles' if family == 'menustyle' else 'animations'}"

    def card(self, item, base: str, target: str | None = None, api: str | None = None, *, detailed: bool = False, v1: bool = False) -> dict:
        version = self.db.one("SELECT * FROM versions WHERE id = ?", (item["current_version_id"],))
        family = item["family"]
        out = {
            "_id": item["id"],
            "id": item["id"],
            "alias": item["alias"],
            "category_id": self.state.catalog.top_category(family, item["category_id"]) if family == "application" and not v1 else item["category_id"],
            "author": item["author"],
            "created_at": int(item["created_at"]),
            "updated_at": int(item["updated_at"]),
            "current_version": self.version_json(item, version, base, target, api, detailed) if version else None,
        }
        if v1:
            out["family"] = family
            out["origin"] = item["origin"]
            out["install_path"] = version["install_path"] if version else item["install_path"]
            out["category_path"] = item["category_id"]
            out["top_category_id"] = self.state.catalog.top_category(family, item["category_id"])
            out["authors"] = loads(item["authors"], [])
            if self.state.stats is not None:
                out["stats"] = self.state.stats.for_item(item["id"])
            if version and family != "animation":
                out["builds"] = [
                    {"target": b["target"], "api": b["api"], "sdk_version": b["sdk_version"], "size": b["size"],
                     "sha256": b["sha256"], "built_at": int(b["built_at"]), "cached": bool(b["artifact_path"])}
                    for b in self.db.all("SELECT * FROM builds WHERE version_id = ? AND result = 'ok' ORDER BY built_at DESC", (version["id"],))
                ]
                if target and api:
                    out["availability"] = self.availability(version["id"], target, api)
        return out

    def category_icon_file(self, row) -> Path | None:
        if not row["icon"]:
            return None
        path = CATEGORY_ICONS / f"{row['icon']}.svg"
        return path if path.is_file() else None

    def category_icon_uri(self, row, base: str) -> str:
        if self.category_icon_file(row) is None:
            return ""
        return f"{base}/api/v0/category/{row['id']}/icon"

    def category_json_v0(self, row, base: str, count: int) -> dict:
        """Exactly the six fields Archivarius returns, in its order."""
        return {
            "_id": row["id"],
            "priority": row["priority"],
            "name": row["name"],
            "color": row["color"],
            "icon_uri": self.category_icon_uri(row, base),
            "applications": count,
        }

    def category_json(self, row, base: str, count: int | None = None) -> dict:
        out = {
            "_id": row["id"],
            "id": row["id"],
            "name": row["name"],
            "color": row["color"],
            "icon_uri": self.category_icon_uri(row, base),
            "icon": row["icon"],
            "priority": row["priority"],
            "parent_id": row["parent_id"],
        }
        if count is not None:
            out["applications"] = count
        return out

    def sdk_list_json(self) -> list[dict]:
        out = []
        for target in self.state.sdk.targets():
            latest = self.state.sdk.latest(target)
            for row in self.db.all("SELECT * FROM sdks WHERE target = ? ORDER BY added_at DESC", (target,)):
                out.append({
                    "id": row["version"],
                    "_id": row["version"],
                    "name": row["version"],
                    "target": row["target"],
                    "channel": row["channel"],
                    "api": row["api"],
                    "is_latest_release": bool(latest and latest.version == row["version"]),
                })
        return out
