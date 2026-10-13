import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..catalog import Catalog
from ..config import Config
from ..db import Database
from .animations import AnimationsSource
from .base import Source
from .manifests import ManifestCatalogSource
from .menustyles import MenuStylesSource

log = logging.getLogger("catalog.sources")


class SourceManager:
    """Owns the sources in priority order and runs the daily pass: the SDK
    index first, then every repository the server reads itself."""

    def __init__(self, cfg: Config, db: Database, catalog: Catalog):
        self.cfg = cfg
        self.db = db
        self.before_pass = None  # async callable run first in every daily pass (SDK index)
        self.sources: list[Source] = []
        # Only repositories nobody uploads for us are read by the server. Our
        # own catalog and (later) ATP send source zips to /api/v1/sources/upload.
        if cfg.ofw_catalog_repo:
            self.sources.append(ManifestCatalogSource(cfg, db, catalog, cfg.ofw_catalog_repo, name="ofw"))
        if cfg.menustyles_repo:
            self.sources.append(MenuStylesSource(cfg, db, catalog, cfg.menustyles_repo))
        if cfg.animations_repo:
            self.sources.append(AnimationsSource(cfg, db, catalog, cfg.animations_repo))
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.last_report: dict = {}

    def set_publish_listener(self, callback) -> None:
        for source in self.sources:
            source.on_published = callback

    def status(self) -> list[dict]:
        return [source.status() for source in self.sources]

    def by_name(self, name: str) -> Source | None:
        return next((s for s in self.sources if s.name == name), None)

    async def check_all(self) -> dict:
        async with self._lock:
            report = {}
            for source in self.sources:
                try:
                    report[source.name] = await asyncio.to_thread(source.check)
                except Exception as e:
                    log.exception("source %s failed: %s", source.name, e)
                    report[source.name] = {"error": str(e)}
            self.last_report = report
            return report

    async def check_one(self, name: str) -> dict:
        source = self.by_name(name)
        if source is None:
            return {"error": "unknown source"}
        async with self._lock:
            try:
                report = await asyncio.to_thread(source.check)
            except Exception as e:
                log.exception("source %s failed: %s", name, e)
                report = {"error": str(e)}
            self.last_report[name] = report
            return report

    @staticmethod
    def seconds_until(hour_utc: int, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def daily_loop(self) -> None:
        while True:
            delay = self.seconds_until(self.cfg.source_check_hour_utc)
            log.info("next source check in %.0f s", delay)
            await asyncio.sleep(delay)
            if self.before_pass is not None:
                try:
                    await self.before_pass()
                except Exception as e:
                    log.error("daily sdk check failed: %s", e)
            try:
                await self.check_all()
            except Exception as e:
                log.error("daily source check crashed: %s", e)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.daily_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
