import json
import logging
import shutil
import urllib.request
from pathlib import Path

from ..bundle_tool import BundleError, build_bundle, category_from_path, checkout_source, read_catalog_manifest
from ..catalog import PublishError
from ..db import dumps, loads
from .base import Source, SourceError

log = logging.getLogger("catalog.sources.manifests")

OFW_API = "https://catalog.flipperzero.one/api/v0"


class ManifestCatalogSource(Source):
    """flipper-application-catalog itself: read once a day, nobody uploads it."""

    def __init__(self, cfg, db, catalog, url, *, name: str):
        self.name = name
        super().__init__(cfg, db, catalog, url)

    # ------------------------------------------------------------------ ofw uid

    def _preferred_id(self, appid: str) -> str | None:
        if self.name != "ofw":
            return None
        cached = self.db.get_meta(f"ofw_uid:{appid}")
        if cached:
            return cached
        try:
            request = urllib.request.Request(f"{OFW_API}/application/{appid}", headers={"User-Agent": "qunleashed-catalog"})
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode())
            uid = str(data.get("_id") or data.get("id") or "")
        except Exception as e:  # the catalogue being down must not block import
            log.warning("ofw uid lookup for %s failed: %s", appid, e)
            return None
        if uid:
            self.db.set_meta(f"ofw_uid:{appid}", uid)
        return uid or None

    # ------------------------------------------------------------------- import

    def import_changed(self, previous, head, initial) -> dict:
        changed, deleted = self.mirror.changed_files(previous, "applications/*/**/manifest.yml")
        # What failed last time is tried again, though its manifest is unchanged:
        # the cause may have been ours, or the author's repository being down.
        retry = [rel for rel in loads(self.db.get_meta(f"failed:{self.name}"), []) if (self.mirror.path / rel).is_file()]
        changed = sorted(set(changed) | set(retry))
        report = {"published": 0, "unchanged": 0, "failed": 0, "hidden": 0, "errors": []}
        failed: list[str] = []
        for rel in changed:
            try:
                result = self.import_manifest(self.mirror.path / rel, initial)
            except Exception as e:  # one failure costs one entry, not the pass
                failed.append(rel)
                report["failed"] += 1
                report["errors"].append(f"{rel}: {e}")
                log.warning("%s: %s failed: %s", self.name, rel, e)
                continue
            if result is None:
                report["unchanged"] += 1
            elif result.created_version:
                report["published"] += 1
            else:
                report["unchanged"] += 1
        for rel in deleted:
            located = category_from_path(Path(rel))
            if located and self.catalog.hide("application", located[1], self.name):
                report["hidden"] += 1
        self.db.set_meta(f"failed:{self.name}", dumps(failed))
        return report

    def import_manifest(self, manifest_path: Path, initial: bool):
        data = read_catalog_manifest(manifest_path)
        located = category_from_path(manifest_path.relative_to(self.mirror.path))
        if located is None:
            raise BundleError("manifest outside applications/")
        category, appid = located
        source = data.get("sourcecode") or {}
        if source.get("type", "git") != "git":
            raise BundleError("only git sources are supported")
        location = source.get("location") or {}
        origin = str(location.get("origin", "")).strip()
        if not origin.startswith("https://") and not (self.cfg.allow_file_origins and origin.startswith("file://")):
            raise BundleError("origin must be an https git url")
        commit = str(location.get("commit_sha", "") or "").strip()
        subdir = str(location.get("subdir", "") or "").strip()
        if commit and self._already_have(appid, commit):
            return None
        checkout_dir = self.work / "checkout" / appid
        checkout = checkout_source(origin, commit or None, subdir, checkout_dir, allow_unpinned=False)
        try:
            manifest = build_bundle(
                family="application",
                source=self.name,
                catalog=data,
                code_dir=checkout.code_dir,
                commit_sha=checkout.commit_sha,
                origin_url=origin,
                subdir=subdir,
                out_dir=self.work / "bundle" / appid,
                path_category=category,
                path_appid=appid,
                raw=False,
            )
            manifest.preferred_id = self._preferred_id(appid)
            manifest.links = {
                "manifest_uri": self._manifest_url(manifest_path),
            }
            return self.publish(manifest, initial)
        finally:
            shutil.rmtree(checkout_dir, ignore_errors=True)
            shutil.rmtree(self.work / "bundle" / appid, ignore_errors=True)

    def _already_have(self, appid: str, commit: str) -> bool:
        """The pinned commit was archived before: nothing to clone. A commit
        fixes the sources, so the version built from it cannot differ."""
        return self.db.one(
            "SELECT 1 FROM versions v JOIN items i ON i.id = v.item_id "
            "WHERE i.family = 'application' AND i.appid = ? AND v.commit_sha = ? AND v.source_origin = ?",
            (appid, commit, self.name),
        ) is not None

    def _manifest_url(self, manifest_path: Path) -> str:
        rel = manifest_path.relative_to(self.mirror.path).as_posix()
        base = self.url.removesuffix(".git")
        if "github.com" in base:
            return f"{base}/blob/main/{rel}"
        return f"{base}/{rel}"
