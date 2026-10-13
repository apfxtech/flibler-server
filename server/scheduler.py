import asyncio
import logging
import time

from .config import Config
from .jobs import PRIORITY_KEEP_WARM, PRIORITY_NEW_SDK, PRIORITY_NEW_VERSION, JobManager
from .sdk_archive import SdkArchive, SdkProfile

log = logging.getLogger("catalog.scheduler")

BUILT_FAMILIES = ("application", "menustyle")


class Scheduler:
    """Turns events into build jobs. Nothing here runs on a timer except the
    hourly keep-warm pass and housekeeping; the sources have their own daily
    pass (section 8.5)."""

    def __init__(self, cfg: Config, state):
        self.cfg = cfg
        self.state = state
        self.db = state.db
        self.jobs: JobManager = state.jobs
        self.sdk: SdkArchive = state.sdk
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._hourly()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    # ------------------------------------------------------------------ events

    def on_published(self, result, family: str) -> None:
        """A new version: build it under the current release and dev SDK of
        every target, then under the other APIs clients asked for lately."""
        if family not in BUILT_FAMILIES or not result.created_version:
            return
        for target in self.sdk.targets():
            seen = set()
            for profile in self.sdk.current(target):
                seen.add(profile.api)
                self.jobs.submit_scheduled(family, result.version_id, target, profile.api, PRIORITY_NEW_VERSION,
                                           "new version", sdk_version=profile.version)
            for api in self.sdk.active_apis(target):
                if api not in seen:
                    self.jobs.submit_scheduled(family, result.version_id, target, api, PRIORITY_NEW_VERSION, "new version")

    def on_new_sdk(self, profile: SdkProfile) -> None:
        """A new SDK: everything visible, most downloaded first."""
        counts = self.state.stats.counts("month") if self.state.stats else {}
        rows = self.db.all(
            "SELECT id, family, current_version_id FROM items WHERE hidden = 0 AND current_version_id IS NOT NULL "
            f"AND family IN ({','.join('?' * len(BUILT_FAMILIES))})",
            BUILT_FAMILIES,
        )
        ordered = sorted(rows, key=lambda r: counts.get(r["id"], 0), reverse=True)
        queued = 0
        for row in ordered:
            if self.jobs.submit_scheduled(row["family"], row["current_version_id"], profile.target, profile.api,
                                          PRIORITY_NEW_SDK, f"new {profile.channel} sdk {profile.version}",
                                          sdk_version=profile.version):
                queued += 1
        log.info("new %s sdk %s: %d builds queued", profile.channel, profile.version, queued)

    def keep_warm(self) -> int:
        """Release builds are held and need nothing. Under the current dev SDK
        the month's top is rebuilt a day before its 7 days run out, for the
        weeks dev does not move."""
        if self.state.stats is None:
            return 0
        queued = 0
        soon = time.time() - (self.cfg.cache_ttl_days - 1) * 86400
        for target in self.sdk.targets():
            latest = self.sdk.latest(target, "dev")
            if latest is None:
                continue
            for item_id, _ in self.state.stats.top(None, None, "month", self.cfg.top_keep_warm):
                item = self.state.catalog.item(item_id)
                if item is None or item["family"] not in BUILT_FAMILIES or not item["current_version_id"]:
                    continue
                version_id = item["current_version_id"]
                build = self.db.one(
                    "SELECT * FROM builds WHERE version_id = ? AND target = ? AND sdk_version = ? AND result = 'ok'",
                    (version_id, target, latest.version),
                )
                if build is None:
                    continue
                if build["artifact_path"] and build["built_at"] > soon:
                    continue
                if self.jobs.submit_scheduled(item["family"], version_id, target, latest.api, PRIORITY_KEEP_WARM,
                                              "keep warm", force=True, sdk_version=latest.version):
                    queued += 1
        return queued

    async def _hourly(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                self.jobs.cleanup_once()
                self.state.catalog.cleanup_bundles()
                self.keep_warm()
                if self.state.push is not None:
                    await self.state.push.flush()
            except Exception as e:
                log.error("hourly pass failed: %s", e)
