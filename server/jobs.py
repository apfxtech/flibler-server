import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .builder import (
    BuildError,
    BundleBuilder,
    BundleDownloadError,
    BundleNotFoundError,
    BundleTimeoutError,
)
from .cache import BuildCache, CacheEntry
from .config import Config
from .sdk import SdkError, SdkManager, SdkProfile

log = logging.getLogger("flibler.jobs")

class JobStatus(str, Enum):
    QUEUED = "queued"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"

class SubmitError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message

def error_code_for(exc: Exception) -> str:
    if isinstance(exc, BundleNotFoundError):
        return "bundle_not_found"
    if isinstance(exc, BundleTimeoutError):
        return "bundle_timeout"
    if isinstance(exc, BundleDownloadError):
        return "bundle_download_failed"
    if isinstance(exc, SdkError):
        return "sdk_error"
    if isinstance(exc, BuildError):
        return "build_failed"
    return "internal_error"

@dataclass
class Job:
    id: str
    client_id: str
    request: dict
    cache_key: str
    profile: SdkProfile
    status: JobStatus = JobStatus.QUEUED
    detail: str = ""
    error: str | None = None
    error_code: str | None = None
    log_tail: str | None = None
    cache_entry: CacheEntry | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def active(self) -> bool:
        return self.status in (JobStatus.QUEUED, JobStatus.BUILDING)

class JobManager:
    def __init__(
        self,
        cfg: Config,
        cache: BuildCache,
        sdk: SdkManager,
        builder: BundleBuilder,
        engine_lock: asyncio.Lock,
    ):
        self.cfg = cfg
        self.cache = cache
        self.sdk = sdk
        self.builder = builder
        self.engine_lock = engine_lock
        self.jobs: dict[str, Job] = {}
        self.queue: deque[str] = deque()
        self.log_dir = cfg.data_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._wakeup = asyncio.Event()
        self._worker_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None

    def start(self) -> None:
        self._worker_task = asyncio.create_task(self._worker())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        for task in (self._worker_task, self._cleanup_task):
            if task:
                task.cancel()

    def queue_position(self, job: Job) -> int | None:
        if job.status != JobStatus.QUEUED:
            return None
        try:
            return list(self.queue).index(job.id) + 1
        except ValueError:
            return None

    def _active_for_client(self, client_id: str) -> Job | None:
        for job in self.jobs.values():
            if job.client_id == client_id and job.active:
                return job
        return None

    def _active_for_key(self, cache_key: str) -> Job | None:
        for job in self.jobs.values():
            if job.cache_key == cache_key and job.active:
                return job
        return None

    def submit(self, request: dict, client_id: str) -> tuple[Job, bool]:
        try:
            profile = self.sdk.profile_for(
                request["index_url"], request["channel"], request["target"]
            )
        except SdkError as e:
            raise SubmitError(400, str(e))

        cache_payload = {**request, "sdk_version": profile.version}
        cache_key = self.cache.key_for(cache_payload)

        cached = self.cache.get(cache_key)
        if cached is not None:
            job = Job(
                id=uuid.uuid4().hex,
                client_id=client_id,
                request=request,
                cache_key=cache_key,
                profile=profile,
                status=JobStatus.READY,
                detail="cached",
                cache_entry=cached,
            )
            job.finished_at = time.time()
            self.jobs[job.id] = job
            return job, False

        existing = self._active_for_key(cache_key)
        if existing is not None and existing.client_id == client_id:
            return existing, False

        active = self._active_for_client(client_id)
        if active is not None:
            if active.request == request:
                return active, False
            raise SubmitError(409, f"Another build is in progress: {active.id}")

        if len(self.queue) >= self.cfg.queue_limit:
            raise SubmitError(429, "Build queue is full")

        job = Job(
            id=uuid.uuid4().hex,
            client_id=client_id,
            request=request,
            cache_key=cache_key,
            profile=profile,
        )
        self.jobs[job.id] = job
        self.queue.append(job.id)
        self._wakeup.set()
        return job, True

    def cancel(self, job: Job) -> bool:
        if job.status != JobStatus.QUEUED:
            return False
        try:
            self.queue.remove(job.id)
        except ValueError:
            return False
        job.status = JobStatus.FAILED
        job.error = "Cancelled"
        job.error_code = "cancelled"
        job.finished_at = time.time()
        return True

    async def _worker(self) -> None:
        while True:
            if not self.queue:
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            job_id = self.queue.popleft()
            job = self.jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED:
                continue
            await self._run_job(job)

    async def _run_job(self, job: Job) -> None:
        job.status = JobStatus.BUILDING
        job.started_at = time.time()
        log_path = self.log_dir / f"{job.id}.log"
        try:
            if not job.profile.deployed:
                job.detail = "deploying sdk"
                await self.sdk.ensure_deployed(job.profile)
                cache_payload = {**job.request, "sdk_version": job.profile.version}
                job.cache_key = self.cache.key_for(cache_payload)
                cached = self.cache.get(job.cache_key)
                if cached is not None:
                    job.cache_entry = cached
                    job.status = JobStatus.READY
                    job.detail = "cached"
                    job.finished_at = time.time()
                    return

            job.detail = "building"
            async with self.engine_lock:
                built = await self.builder.build(
                    job.id, job.request["bundle_url"], job.profile, log_path
                )
            job.cache_entry = self.cache.put(
                job.cache_key,
                built.path,
                {
                    "request": job.request,
                    "appid": built.appid,
                    "sdk_version": job.profile.version,
                },
            )
            job.status = JobStatus.READY
            job.detail = "built"
            log.info("job %s ready: %s", job.id, built.path.name)
        except (BuildError, SdkError) as e:
            job.status = JobStatus.FAILED
            job.error = str(e)
            job.error_code = error_code_for(e)
            job.log_tail = self._read_log_tail(log_path)
            log.warning("job %s failed: %s", job.id, e)
        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = f"Internal error: {e}"
            job.error_code = "internal_error"
            job.log_tail = self._read_log_tail(log_path)
            log.exception("job %s crashed", job.id)
        finally:
            job.finished_at = time.time()
            self.builder.cleanup_job(job.id)

    @staticmethod
    def _read_log_tail(log_path: Path, size: int = 4000) -> str | None:
        try:
            data = log_path.read_bytes()
        except OSError:
            return None
        return data[-size:].decode(errors="replace")

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                self._cleanup_once()
            except Exception as e:
                log.error("cleanup failed: %s", e)

    def _cleanup_once(self) -> None:
        cutoff = time.time() - self.cfg.job_ttl_hours * 3600
        for job_id, job in list(self.jobs.items()):
            if not job.active and (job.finished_at or job.created_at) < cutoff:
                del self.jobs[job_id]
                (self.log_dir / f"{job_id}.log").unlink(missing_ok=True)
        self.cache.cleanup()
