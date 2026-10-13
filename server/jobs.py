import asyncio
import hashlib
import logging
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .archive import Archive
from .builder import (
    BuildCancelled,
    BuildError,
    Builder,
    BundleDownloadError,
    BundleNotFoundError,
    BundleTimeoutError,
)
from .config import Config
from .db import Database
from .sdk_archive import SdkArchive, SdkError, SdkProfile

log = logging.getLogger("catalog.jobs")

PRIORITY_USER = 0
PRIORITY_NEW_VERSION = 10
PRIORITY_NEW_SDK = 20
PRIORITY_KEEP_WARM = 30

STATUS_QUEUED = "queued"
STATUS_BUILDING = "building"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

ACTIVE = (STATUS_QUEUED, STATUS_BUILDING)


class SubmitError(Exception):
    def __init__(self, status_code: int, message: str, error_code: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_code = error_code


def error_code_for(exc: Exception) -> str:
    if isinstance(exc, BundleNotFoundError):
        return "bundle_not_found"
    if isinstance(exc, BundleTimeoutError):
        return "bundle_timeout"
    if isinstance(exc, BundleDownloadError):
        return "bundle_download_failed"
    if isinstance(exc, BuildCancelled):
        return "cancelled"
    if isinstance(exc, SdkError):
        return "sdk_error"
    if isinstance(exc, BuildError):
        return "build_failed"
    return "internal_error"


def cache_key(family: str, version_id: str, target: str, sdk_version: str) -> str:
    return f"{family}:{version_id}:{target}:{sdk_version}"


def url_cache_key(bundle_url: str, target: str, sdk_version: str) -> str:
    raw = f"{bundle_url}|{target}|{sdk_version}".encode()
    return "url:" + hashlib.sha256(raw).hexdigest()


class JobManager:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        sdk: SdkArchive,
        builder: Builder,
        archives: dict[str, Archive],
    ):
        self.cfg = cfg
        self.db = db
        self.sdk = sdk
        self.builder = builder
        self.archives = archives
        self.cache_root = cfg.cache_root
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.logs_root = cfg.logs_root
        self.logs_root.mkdir(parents=True, exist_ok=True)
        self._wakeup = asyncio.Event()
        self._changed = asyncio.Condition()
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self.on_build_done = None  # async callback(job_row) for the scheduler/push

    # ---------------------------------------------------------------- lifecycle

    def _kick(self) -> None:
        """Wakes the worker from any thread: submit() is called from request
        handlers, from the scheduler and from the sources' worker threads."""
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._wakeup.set()
        else:
            loop.call_soon_threadsafe(self._wakeup.set)

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.db.execute(
            "UPDATE jobs SET status = ?, detail = 'requeued after restart' WHERE status = ?",
            (STATUS_QUEUED, STATUS_BUILDING),
        )
        for _ in range(max(1, self.cfg.workers)):
            self._tasks.append(asyncio.create_task(self._worker()))
        self._tasks.append(asyncio.create_task(self._cleanup_loop()))
        self._kick()

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    # -------------------------------------------------------------------- views

    def job(self, job_id: str):
        return self.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))

    def queue_position(self, row) -> int | None:
        if row["status"] != STATUS_QUEUED:
            return None
        ahead = self.db.scalar(
            "SELECT COUNT(*) FROM jobs WHERE status = ? AND "
            "(priority < ? OR (priority = ? AND seq < ?))",
            (STATUS_QUEUED, row["priority"], row["priority"], row["seq"]),
        )
        return int(ahead) + 1

    def queue_length(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM jobs WHERE status = ?", (STATUS_QUEUED,)))

    def job_view(self, row) -> dict:
        view = {
            "id": row["id"],
            "status": row["status"],
            "detail": row["detail"],
            "queue_position": self.queue_position(row),
            "sdk_version": row["sdk_version"],
            "api": row["api"],
            "target": row["target"],
            "family": row["family"],
        }
        if row["error"]:
            view["error"] = row["error"]
        if row["error_code"]:
            view["error_code"] = row["error_code"]
        if row["log_tail"]:
            view["log_tail"] = row["log_tail"]
        if row["artifact_path"]:
            path = Path(row["artifact_path"])
            view["fap_name"] = row["artifact_name"]
            view["fap_size"] = path.stat().st_size if path.is_file() else None
        return view

    def is_subscriber(self, job_id: str, client_id: str) -> bool:
        return (
            self.db.one(
                "SELECT 1 FROM job_subscribers WHERE job_id = ? AND client_id = ?",
                (job_id, client_id),
            )
            is not None
        )

    # ------------------------------------------------------------------ history

    def ready_build(self, version_id: str, target: str, api: str):
        """The newest successful build for this API whose artifact is still on
        disk, or None. A row without a file is an expired build: it still
        counts for visibility (section 8.1), not for download."""
        rows = self.db.all(
            "SELECT * FROM builds WHERE version_id = ? AND target = ? AND api = ? "
            "AND result = 'ok' AND artifact_path IS NOT NULL ORDER BY built_at DESC",
            (version_id, target, api),
        )
        for row in rows:
            if Path(row["artifact_path"]).is_file():
                return row
        return None

    def build_state(self, version_id: str, target: str, api: str) -> str:
        """ready | expired | failed | never, as section 8.1 defines them."""
        if self.ready_build(version_id, target, api) is not None:
            return "ready"
        ok = self.db.one(
            "SELECT 1 FROM builds WHERE version_id = ? AND target = ? AND api = ? AND result = 'ok'",
            (version_id, target, api),
        )
        if ok is not None:
            return "expired"
        failed = self.db.one(
            "SELECT 1 FROM builds WHERE version_id = ? AND target = ? AND api = ? AND result = 'failed'",
            (version_id, target, api),
        )
        return "failed" if failed is not None else "never"

    def failed_for_sdk(self, version_id: str, target: str, sdk_version: str):
        return self.db.one(
            "SELECT * FROM builds WHERE version_id = ? AND target = ? AND sdk_version = ? AND result = 'failed'",
            (version_id, target, sdk_version),
        )

    # ------------------------------------------------------------------- submit

    def _resolve_profile(self, target: str, api: str | None) -> SdkProfile:
        if api:
            profile = self.sdk.profile_for(target, api)
            if profile is None:
                raise SubmitError(400, f"No SDK for api {api} on {target}", "unknown_api")
        else:
            profile = self.sdk.latest(target)
            if profile is None:
                raise SubmitError(400, f"No SDK for target {target}", "unknown_target")
        return profile

    def _version_row(self, version_id: str):
        row = self.db.one("SELECT * FROM versions WHERE id = ?", (version_id,))
        if row is None:
            raise SubmitError(404, "Unknown version", "unknown_version")
        return row

    def _active_for_key(self, key: str):
        return self.db.one(
            "SELECT * FROM jobs WHERE cache_key = ? AND status IN (?, ?) ORDER BY seq LIMIT 1",
            (key, STATUS_QUEUED, STATUS_BUILDING),
        )

    def _insert_job(self, fields: dict, client_id: str | None):
        job_id = uuid.uuid4().hex
        row = {
            "id": job_id,
            "cache_key": fields["cache_key"],
            "family": fields.get("family", "application"),
            "item_id": fields.get("item_id"),
            "version_id": fields.get("version_id"),
            "appid": fields.get("appid", ""),
            "target": fields["target"],
            "api": fields.get("api", ""),
            "sdk_version": fields.get("sdk_version", ""),
            "bundle_url": fields.get("bundle_url", ""),
            "priority": fields.get("priority", PRIORITY_USER),
            "origin": fields.get("origin", "user"),
            "status": fields.get("status", STATUS_QUEUED),
            "detail": fields.get("detail", ""),
            "artifact_path": fields.get("artifact_path"),
            "artifact_name": fields.get("artifact_name"),
            "created_at": time.time(),
            "finished_at": fields.get("finished_at"),
            "seq": self.db.next_seq(),
        }
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO jobs (id, cache_key, family, item_id, version_id, appid, target, api, "
                "sdk_version, bundle_url, priority, origin, status, detail, artifact_path, "
                "artifact_name, created_at, finished_at, seq) VALUES (:id, :cache_key, :family, "
                ":item_id, :version_id, :appid, :target, :api, :sdk_version, :bundle_url, :priority, "
                ":origin, :status, :detail, :artifact_path, :artifact_name, :created_at, "
                ":finished_at, :seq)",
                row,
            )
            if client_id:
                conn.execute(
                    "INSERT OR IGNORE INTO job_subscribers (job_id, client_id) VALUES (?, ?)",
                    (job_id, client_id),
                )
        return self.job(job_id)

    def submit_user(self, request: dict, client_id: str) -> tuple:
        """A signed client request. Returns (job_row, created)."""
        target = request["target"]
        api = request.get("api") or None
        family = request.get("family") or "application"
        profile = self._resolve_profile(target, api)
        version_id = request.get("version_id")
        bundle_url = request.get("bundle_url", "")

        if version_id:
            version = self._version_row(version_id)
            family = self.db.scalar("SELECT family FROM items WHERE id = ?", (version["item_id"],)) or family
            key = cache_key(family, version_id, target, profile.version)
            ready = self.ready_build(version_id, target, profile.api)
            if ready is not None:
                self.sdk.touch(target, profile.api)
                job = self._insert_job(
                    {
                        "cache_key": key,
                        "family": family,
                        "item_id": version["item_id"],
                        "version_id": version_id,
                        "target": target,
                        "api": profile.api,
                        "sdk_version": ready["sdk_version"],
                        "status": STATUS_READY,
                        "detail": "cached",
                        "artifact_path": ready["artifact_path"],
                        "artifact_name": ready["artifact_name"],
                        "finished_at": time.time(),
                    },
                    client_id,
                )
                return job, False
            failed = self.failed_for_sdk(version_id, target, profile.version)
            if failed is not None:
                raise SubmitError(409, failed["error"] or "Build failed for this SDK", "build_failed")
            fields = {
                "cache_key": key,
                "family": family,
                "item_id": version["item_id"],
                "version_id": version_id,
                "appid": request.get("appid", ""),
            }
        elif bundle_url:
            parsed = urlparse(bundle_url)
            if parsed.scheme != "https" or parsed.hostname not in self.cfg.bundle_hosts():
                raise SubmitError(400, "Invalid bundle_url", "invalid_bundle_url")
            key = url_cache_key(bundle_url, target, profile.version)
            fields = {
                "cache_key": key,
                "family": family,
                "bundle_url": bundle_url,
                "appid": request.get("appid", ""),
            }
            cached = self._cached_url_job(key)
            if cached is not None:
                job = self._insert_job(
                    {**fields, "target": target, "api": profile.api, "sdk_version": profile.version,
                     "status": STATUS_READY, "detail": "cached",
                     "artifact_path": cached["artifact_path"], "artifact_name": cached["artifact_name"],
                     "finished_at": time.time()},
                    client_id,
                )
                return job, False
        else:
            raise SubmitError(400, "Either version_id or bundle_url is required", "invalid_request")

        existing = self._active_for_key(key)
        if existing is not None:
            self.db.execute(
                "INSERT INTO job_subscribers (job_id, client_id, cancelled) VALUES (?, ?, 0) "
                "ON CONFLICT(job_id, client_id) DO UPDATE SET cancelled = 0",
                (existing["id"], client_id),
            )
            if existing["priority"] > PRIORITY_USER and existing["status"] == STATUS_QUEUED:
                # A scheduler job somebody is now waiting for moves to the front.
                self.db.execute(
                    "UPDATE jobs SET priority = ?, seq = ? WHERE id = ?",
                    (PRIORITY_USER, self.db.next_seq(), existing["id"]),
                )
            return self.job(existing["id"]), False

        if self.queue_length() >= self.cfg.queue_limit:
            raise SubmitError(429, "Build queue is full", "queue_full")

        self.sdk.touch(target, profile.api)
        job = self._insert_job(
            {**fields, "target": target, "api": profile.api, "sdk_version": profile.version,
             "priority": PRIORITY_USER, "origin": "user"},
            client_id,
        )
        self._kick()
        return job, True

    def _cached_url_job(self, key: str):
        cutoff = time.time() - self.cfg.cache_ttl_days * 86400
        rows = self.db.all(
            "SELECT * FROM jobs WHERE cache_key = ? AND status = ? AND artifact_path IS NOT NULL "
            "AND finished_at >= ? ORDER BY finished_at DESC",
            (key, STATUS_READY, cutoff),
        )
        for row in rows:
            if Path(row["artifact_path"]).is_file():
                return row
        return None

    def submit_scheduled(
        self, family: str, version_id: str, target: str, api: str, priority: int, reason: str,
        force: bool = False, sdk_version: str | None = None,
    ):
        """A build the server wants for itself. Skipped when the result is
        already known for this SDK, or a job for it is already active."""
        profile = (
            self.sdk.profile_for_version(target, sdk_version) if sdk_version else self.sdk.profile_for(target, api)
        )
        if profile is None:
            return None
        version = self.db.one("SELECT * FROM versions WHERE id = ?", (version_id,))
        if version is None:
            return None
        known = self.db.one(
            "SELECT result, artifact_path FROM builds WHERE version_id = ? AND target = ? AND sdk_version = ?",
            (version_id, target, profile.version),
        )
        if known is not None:
            if known["result"] == "failed":
                return None
            if not force and known["artifact_path"] and Path(known["artifact_path"]).is_file():
                return None
        key = cache_key(family, version_id, target, profile.version)
        if self._active_for_key(key) is not None:
            return None
        job = self._insert_job(
            {
                "cache_key": key,
                "family": family,
                "item_id": version["item_id"],
                "version_id": version_id,
                "target": target,
                "api": profile.api,
                "sdk_version": profile.version,
                "priority": priority,
                "origin": "scheduler",
                "detail": reason,
            },
            None,
        )
        self._kick()
        return job

    # ------------------------------------------------------------------- cancel

    def cancel(self, job_id: str, client_id: str) -> bool:
        row = self.job(job_id)
        if row is None:
            return False
        self.db.execute(
            "UPDATE job_subscribers SET cancelled = 1 WHERE job_id = ? AND client_id = ?",
            (job_id, client_id),
        )
        if row["status"] not in ACTIVE:
            return True
        left = self.db.scalar(
            "SELECT COUNT(*) FROM job_subscribers WHERE job_id = ? AND cancelled = 0", (job_id,)
        )
        if int(left) > 0 or row["origin"] != "user":
            return True
        if row["status"] == STATUS_QUEUED:
            self._finish(job_id, STATUS_FAILED, error="Cancelled", error_code="cancelled")
            return True
        self.builder.kill(job_id)
        return True

    # ------------------------------------------------------------------- worker

    def _take_next(self):
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY priority, seq LIMIT 1",
                (STATUS_QUEUED,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET status = ?, started_at = ?, detail = 'starting' WHERE id = ?",
                (STATUS_BUILDING, time.time(), row["id"]),
            )
        return self.job(row["id"])

    async def _worker(self) -> None:
        while True:
            job = self._take_next()
            if job is None:
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            await self._notify()
            try:
                await self._run_job(job)
            except Exception as e:  # the job is already marked, keep the worker alive
                log.exception("job %s crashed outside the handler: %s", job["id"], e)

    async def _set_detail(self, job_id: str, detail: str) -> None:
        self.db.execute("UPDATE jobs SET detail = ? WHERE id = ?", (detail, job_id))
        await self._notify()

    async def _run_job(self, job) -> None:
        job_id = job["id"]
        log_path = self.logs_root / f"{job_id}.log"
        profile = self.sdk.profile_for_version(job["target"], job["sdk_version"])
        try:
            if profile is None:
                raise SdkError(f"SDK {job['sdk_version']} for {job['target']} is gone")
            if not profile.deployed:
                await self._set_detail(job_id, "deploying sdk")
                await self.sdk.ensure_deployed(profile)
            await self._set_detail(job_id, "building")
            if job["version_id"]:
                artifact = await self._build_version(job, profile, log_path)
            else:
                artifact = await self.builder.build_url(
                    job_id, job["bundle_url"], profile, job["family"], job["appid"] or None, log_path
                )
            stored_dir = self.cache_root / job["family"] / (job["version_id"] or job["cache_key"]) / job["target"] / profile.version
            stored_dir.mkdir(parents=True, exist_ok=True)
            stored = stored_dir / artifact.path.name
            shutil.copy2(artifact.path, stored)
            if job["version_id"]:
                self.db.execute(
                    "INSERT INTO builds (version_id, target, api, sdk_version, result, artifact_path, "
                    "artifact_name, sha256, size, built_at) VALUES (?, ?, ?, ?, 'ok', ?, ?, ?, ?, ?) "
                    "ON CONFLICT(version_id, target, api, sdk_version) DO UPDATE SET result = 'ok', "
                    "error = NULL, log_tail = NULL, artifact_path = excluded.artifact_path, "
                    "artifact_name = excluded.artifact_name, sha256 = excluded.sha256, "
                    "size = excluded.size, built_at = excluded.built_at",
                    (job["version_id"], job["target"], profile.api, profile.version, str(stored),
                     stored.name, artifact.sha256, artifact.size, time.time()),
                )
            self._finish(job_id, STATUS_READY, detail="built", artifact_path=str(stored), artifact_name=stored.name)
            log.info("job %s ready: %s", job_id, stored.name)
        except asyncio.CancelledError:
            raise
        except BuildCancelled:
            self._finish(job_id, STATUS_FAILED, error="Cancelled", error_code="cancelled")
        except (BuildError, SdkError) as e:
            tail = self.builder.scrub(self._read_log_tail(log_path))
            message = self.builder.scrub(str(e))
            if job["version_id"] and not isinstance(e, (BundleDownloadError, BundleTimeoutError)):
                self.db.execute(
                    "INSERT INTO builds (version_id, target, api, sdk_version, result, error, log_tail, built_at) "
                    "VALUES (?, ?, ?, ?, 'failed', ?, ?, ?) ON CONFLICT(version_id, target, api, sdk_version) "
                    "DO UPDATE SET result = 'failed', error = excluded.error, log_tail = excluded.log_tail, "
                    "artifact_path = NULL, built_at = excluded.built_at",
                    (job["version_id"], job["target"], profile.api if profile else job["api"], job["sdk_version"], message, tail, time.time()),
                )
            self._finish(job_id, STATUS_FAILED, error=message, error_code=error_code_for(e), log_tail=tail)
            log.warning("job %s failed: %s", job_id, message)
        except Exception as e:
            tail = self.builder.scrub(self._read_log_tail(log_path))
            self._finish(job_id, STATUS_FAILED, error=f"Internal error: {e}", error_code="internal_error", log_tail=tail)
            log.exception("job %s crashed", job_id)
        finally:
            self.builder.cleanup_job(job_id)
            if self._tasks and not asyncio.current_task().cancelling():
                await self._after_job(job_id)

    async def _after_job(self, job_id: str) -> None:
        await self._notify()
        if self.on_build_done is None:
            return
        try:
            await self.on_build_done(self.job(job_id))
        except Exception as e:
            log.exception("on_build_done failed: %s", e)

    async def _build_version(self, job, profile: SdkProfile, log_path: Path):
        version = self.db.one("SELECT * FROM versions WHERE id = ?", (job["version_id"],))
        if version is None:
            raise BuildError("Version disappeared")
        archive = self.archives[job["family"]]
        workdir = self.builder.workdir(job["id"])
        shutil.rmtree(workdir, ignore_errors=True)
        source_root = workdir / "src"
        await asyncio.to_thread(archive.export_tree, version["tag"], version["archive_path"], source_root)
        appid = self.db.scalar("SELECT appid FROM items WHERE id = ?", (version["item_id"],))
        return await self.builder.build_tree(job["id"], source_root, profile, job["family"], appid, log_path)

    def _finish(self, job_id: str, status: str, **fields) -> None:
        sets = ["status = ?", "finished_at = ?"]
        params: list = [status, time.time()]
        for key in ("detail", "error", "error_code", "log_tail", "artifact_path", "artifact_name"):
            if key in fields:
                sets.append(f"{key} = ?")
                params.append(fields[key])
        params.append(job_id)
        self.db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", tuple(params))

    @staticmethod
    def _read_log_tail(log_path: Path, size: int = 4000) -> str | None:
        try:
            data = log_path.read_bytes()
        except OSError:
            return None
        return data[-size:].decode(errors="replace")

    # ------------------------------------------------------------------- events

    async def _notify(self) -> None:
        async with self._changed:
            self._changed.notify_all()

    async def events(self, job_id: str, keepalive: float = 15.0):
        """Yields the job view on every change, ending with a final state.
        Yields None on keepalive ticks so the stream can send a comment."""
        last = None
        while True:
            row = self.job(job_id)
            if row is None:
                return
            view = self.job_view(row)
            if view != last:
                last = view
                yield view
            if row["status"] in (STATUS_READY, STATUS_FAILED):
                return
            async with self._changed:
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=keepalive)
                except asyncio.TimeoutError:
                    yield None

    # ------------------------------------------------------------------ cleanup

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                self.cleanup_once()
            except Exception as e:
                log.error("cleanup failed: %s", e)

    def cleanup_once(self) -> dict:
        now = time.time()
        expired = 0
        cutoff = now - self.cfg.cache_ttl_days * 86400
        held = self.sdk.held_versions()
        for row in self.db.all(
            "SELECT version_id, target, api, sdk_version, artifact_path FROM builds "
            "WHERE artifact_path IS NOT NULL AND built_at < ?",
            (cutoff,),
        ):
            if (row["target"], row["sdk_version"]) in held:
                continue  # the current release is built once and kept
            path = Path(row["artifact_path"])
            shutil.rmtree(path.parent, ignore_errors=True)
            self.db.execute(
                "UPDATE builds SET artifact_path = NULL WHERE version_id = ? AND target = ? AND api = ? AND sdk_version = ?",
                (row["version_id"], row["target"], row["api"], row["sdk_version"]),
            )
            expired += 1
        for row in self.db.all(
            "SELECT id, artifact_path, cache_key FROM jobs WHERE status = ? AND version_id IS NULL "
            "AND artifact_path IS NOT NULL AND finished_at < ?",
            (STATUS_READY, cutoff),
        ):
            shutil.rmtree(Path(row["artifact_path"]).parent, ignore_errors=True)
            self.db.execute("UPDATE jobs SET artifact_path = NULL WHERE id = ?", (row["id"],))
        removed = 0
        job_cutoff = now - self.cfg.job_ttl_hours * 3600
        for row in self.db.all(
            "SELECT id FROM jobs WHERE status IN (?, ?) AND COALESCE(finished_at, created_at) < ?",
            (STATUS_READY, STATUS_FAILED, job_cutoff),
        ):
            with self.db.tx() as conn:
                conn.execute("DELETE FROM job_subscribers WHERE job_id = ?", (row["id"],))
                conn.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
            (self.logs_root / f"{row['id']}.log").unlink(missing_ok=True)
            removed += 1
        return {"expired_artifacts": expired, "removed_jobs": removed}
