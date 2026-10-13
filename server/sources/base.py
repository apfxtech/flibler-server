import logging
import shutil
import subprocess
import time
from pathlib import Path

from ..catalog import Catalog, Manifest, PublishError, PublishResult
from ..config import Config
from ..db import Database, loads

log = logging.getLogger("catalog.sources")


class SourceError(Exception):
    pass


def run_git(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if result.returncode != 0:
        raise SourceError(f"git {' '.join(args[:2])}: {result.stderr.strip()[:400]}")
    return result.stdout


class RepoMirror:
    """A local clone of a source repository that is fetched, never edited."""

    def __init__(self, root: Path, name: str, url: str):
        self.name = name
        self.url = url
        self.path = root / name

    def sync(self) -> str:
        if not (self.path / ".git").is_dir():
            shutil.rmtree(self.path, ignore_errors=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            run_git(["clone", "-q", "--filter=blob:none", self.url, str(self.path)])
        else:
            run_git(["fetch", "-q", "--prune", "origin"], self.path)
            run_git(["reset", "-q", "--hard", "origin/HEAD"], self.path)
        return self.head()

    def head(self) -> str:
        return run_git(["rev-parse", "HEAD"], self.path).strip()

    def changed_files(self, since: str | None, pattern: str) -> tuple[list[str], list[str]]:
        """(changed_or_added, deleted) paths matching the glob since a commit.
        Without a base commit everything that matches is 'changed'."""
        if not since:
            out = run_git(["ls-files", "--", pattern], self.path)
            return [line for line in out.splitlines() if line], []
        try:
            out = run_git(["diff", "--name-status", since, "HEAD", "--", pattern], self.path)
        except SourceError:
            out = run_git(["ls-files", "--", pattern], self.path)
            return [line for line in out.splitlines() if line], []
        changed, deleted = [], []
        for line in out.splitlines():
            status, _, path = line.partition("\t")
            if not path:
                continue
            if status.startswith("D"):
                deleted.append(path)
            else:
                changed.append(path.split("\t")[-1])
        return changed, deleted

    def files(self, pattern: str) -> list[str]:
        out = run_git(["ls-files", "--", pattern], self.path)
        return [line for line in out.splitlines() if line]


class Source:
    """One ingestion source. Subclasses fill import_changed()."""

    name = ""
    family = "application"

    def __init__(self, cfg: Config, db: Database, catalog: Catalog, url: str):
        self.cfg = cfg
        self.db = db
        self.catalog = catalog
        self.url = url
        self.mirror = RepoMirror(cfg.sources_root, self.name, url)
        self.work = cfg.work_root / "sources" / self.name
        self.on_published = None  # callback(PublishResult, family)

    # state kept in the sources table
    def state(self):
        return self.db.one("SELECT * FROM sources WHERE id = ?", (self.name,))

    def _save_state(self, commit: str) -> None:
        self.db.execute(
            "INSERT INTO sources (id, repo_url, last_commit, last_checked, imported) VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(id) DO UPDATE SET repo_url = excluded.repo_url, last_commit = excluded.last_commit, "
            "last_checked = excluded.last_checked, imported = 1",
            (self.name, self.url, commit, time.time()),
        )

    @property
    def imported_before(self) -> bool:
        row = self.state()
        return bool(row and row["imported"])

    def status(self) -> dict:
        row = self.state()
        origin = "unleashed" if self.family != "application" else self.name
        items = self.db.one(
            "SELECT COUNT(*) AS total, COALESCE(SUM(hidden), 0) AS hidden FROM items WHERE family = ? AND origin = ?",
            (self.family, origin),
        )
        artifacts = {}
        for build in self.db.all(
            "SELECT b.sdk_version, b.api, COALESCE(s.channel, '') AS channel, "
            "SUM(b.result = 'ok') AS built, SUM(b.result = 'failed') AS failed, "
            "SUM(b.result = 'ok' AND b.artifact_path IS NOT NULL) AS cached "
            "FROM builds b JOIN versions v ON v.id = b.version_id JOIN items i ON i.id = v.item_id "
            "LEFT JOIN sdks s ON s.target = b.target AND s.version = b.sdk_version "
            "WHERE i.family = ? AND i.origin = ? AND i.current_version_id = b.version_id "
            "GROUP BY b.sdk_version, b.api, s.channel ORDER BY MAX(b.built_at) DESC",
            (self.family, origin),
        ):
            artifacts[build["sdk_version"]] = {
                "channel": build["channel"],
                "api": build["api"],
                "built": int(build["built"] or 0),
                "failed": int(build["failed"] or 0),
                "cached": int(build["cached"] or 0),
            }
        failed_entries = loads(self.db.get_meta(f"failed:{self.name}"), [])
        return {
            "id": self.name,
            "family": self.family,
            "repo": self.url,
            "last_commit": row["last_commit"] if row else None,
            "last_checked": row["last_checked"] if row else None,
            "items": int(items["total"] or 0) - int(items["hidden"] or 0),
            "hidden": int(items["hidden"] or 0),
            "failed_entries": len(failed_entries),
            "artifacts": artifacts,
        }

    def publish(self, manifest: Manifest, initial: bool) -> PublishResult | None:
        result = self.catalog.publish(manifest, initial_import=initial)
        if result is not None and self.on_published is not None:
            self.on_published(result, manifest.family)
        return result

    def check(self) -> dict:
        """Fetches the repository and imports what changed. One failure costs
        one manifest, not the pass."""
        if not self.url:
            return {"skipped": "no repository configured"}
        initial = not self.imported_before
        previous = self.state()["last_commit"] if self.state() else None
        head = self.mirror.sync()
        report = self.import_changed(previous, head, initial)
        self._save_state(head)
        report["commit"] = head
        return report

    def import_changed(self, previous: str | None, head: str, initial: bool) -> dict:
        raise NotImplementedError
