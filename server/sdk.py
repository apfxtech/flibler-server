import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config

log = logging.getLogger("flibler.sdk")

CHANNEL_INDEX_IDS = {
    "release": "release",
    "rc": "release-candidate",
    "dev": "development",
}

WARMUP_MANIFEST = """App(
    appid="flibler_warmup",
    name="Flibler Warmup",
    apptype=FlipperAppType.EXTERNAL,
    entry_point="flibler_warmup_app",
    stack_size=1 * 1024,
    fap_category="Misc",
)
"""

WARMUP_SOURCE = """#include <furi.h>

int32_t flibler_warmup_app(void* p) {
    UNUSED(p);
    return 0;
}
"""


class SdkError(Exception):
    pass


def profile_key(index_url: str, channel: str, target: str) -> str:
    raw = f"{index_url}|{channel}|{target}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class SdkProfile:
    index_url: str
    channel: str
    target: str
    home: Path

    @property
    def key(self) -> str:
        return profile_key(self.index_url, self.channel, self.target)

    @property
    def state_file(self) -> Path:
        return self.home / "current" / "ufbt_state.json"

    @property
    def deployed(self) -> bool:
        return self.state_file.is_file()

    @property
    def version(self) -> str | None:
        if not self.deployed:
            return None
        try:
            return json.loads(self.state_file.read_text()).get("version")
        except (OSError, json.JSONDecodeError):
            return None


def _fetch_json(url: str, timeout: int = 30) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "uFBT SDKLoader/0.2"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


class SdkManager:
    def __init__(self, cfg: Config, engine_lock: asyncio.Lock):
        self.cfg = cfg
        self.engine_lock = engine_lock
        self.sdk_root = cfg.data_dir / "sdk"
        self.registry_file = self.sdk_root / "profiles.json"
        self.sdk_root.mkdir(parents=True, exist_ok=True)
        self._registry: dict[str, dict] = self._load_registry()

    def _load_registry(self) -> dict[str, dict]:
        if not self.registry_file.is_file():
            return {}
        try:
            return json.loads(self.registry_file.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_registry(self) -> None:
        self.registry_file.write_text(json.dumps(self._registry, indent=2))

    def profile_for(self, index_url: str, channel: str, target: str) -> SdkProfile:
        if channel not in CHANNEL_INDEX_IDS:
            raise SdkError(f"Unknown channel: {channel}")
        if index_url not in self.cfg.allowed_index_urls:
            raise SdkError("Index URL is not allowed")
        profile = SdkProfile(
            index_url=index_url,
            channel=channel,
            target=target,
            home=self.sdk_root / profile_key(index_url, channel, target),
        )
        self._registry[profile.key] = {
            "index_url": index_url,
            "channel": channel,
            "target": target,
            "last_used": time.time(),
        }
        self._save_registry()
        return profile

    def _profile_from_entry(self, entry: dict) -> SdkProfile:
        return SdkProfile(
            index_url=entry["index_url"],
            channel=entry["channel"],
            target=entry["target"],
            home=self.sdk_root
            / profile_key(entry["index_url"], entry["channel"], entry["target"]),
        )

    async def latest_version(self, profile: SdkProfile) -> str:
        index = await asyncio.to_thread(_fetch_json, profile.index_url)
        channel_id = CHANNEL_INDEX_IDS[profile.channel]
        for channel in index.get("channels", []):
            if channel.get("id") == channel_id:
                versions = channel.get("versions", [])
                if not versions:
                    raise SdkError(f"Empty channel: {channel_id}")
                return versions[0]["version"]
        raise SdkError(f"Channel not found in index: {channel_id}")

    async def _run_logged(
        self, args: list[str], env: dict, cwd: Path, timeout: int, label: str
    ) -> None:
        log.info("%s: %s", label, " ".join(args))
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise SdkError(f"{label} timed out after {timeout}s")
        tail = output.decode(errors="replace")[-4000:]
        if process.returncode != 0:
            raise SdkError(f"{label} failed (exit {process.returncode}):\n{tail}")
        log.debug("%s output tail:\n%s", label, tail)

    def _env_for(self, profile: SdkProfile) -> dict:
        env = dict(os.environ)
        env["UFBT_HOME"] = str(profile.home)
        env["UFBT_STATE_DIR"] = str(profile.home)
        env["FBT_TOOLCHAIN_PATH"] = str(profile.home)
        env.pop("FBT_TOOLCHAIN_VERSION", None)
        return env

    async def deploy(self, profile: SdkProfile) -> None:
        profile.home.mkdir(parents=True, exist_ok=True)
        args = [
            sys.executable,
            "-m",
            "ufbt",
            "update",
            "--channel",
            profile.channel,
            "--index-url",
            profile.index_url,
            "--hw-target",
            profile.target,
        ]
        await self._run_logged(
            args,
            self._env_for(profile),
            profile.home,
            self.cfg.sdk_deploy_timeout_sec,
            f"sdk deploy {profile.key}",
        )
        await self._warmup(profile)

    async def _warmup(self, profile: SdkProfile) -> None:
        workdir = Path(tempfile.mkdtemp(prefix="warmup-", dir=str(profile.home)))
        try:
            (workdir / "application.fam").write_text(WARMUP_MANIFEST)
            (workdir / "flibler_warmup.c").write_text(WARMUP_SOURCE)
            await self._run_logged(
                [sys.executable, "-m", "ufbt"],
                self._env_for(profile),
                workdir,
                self.cfg.sdk_deploy_timeout_sec,
                f"sdk warmup {profile.key}",
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def ensure_deployed(self, profile: SdkProfile) -> None:
        if profile.deployed:
            return
        async with self.engine_lock:
            if not profile.deployed:
                await self.deploy(profile)

    async def refresh_profile(self, profile: SdkProfile) -> bool:
        try:
            latest = await self.latest_version(profile)
        except Exception as e:
            log.warning("index check failed for %s: %s", profile.key, e)
            return False
        current = profile.version
        if current == latest and profile.deployed:
            log.info("sdk %s is up-to-date (%s)", profile.key, current)
            return False
        log.info("sdk %s: %s -> %s, deploying", profile.key, current, latest)
        async with self.engine_lock:
            await self.deploy(profile)
        return True

    async def refresh_all(self) -> None:
        cutoff = time.time() - self.cfg.sdk_profile_idle_days * 86400
        for key, entry in list(self._registry.items()):
            if entry.get("last_used", 0) < cutoff:
                profile = self._profile_from_entry(entry)
                shutil.rmtree(profile.home, ignore_errors=True)
                del self._registry[key]
                self._save_registry()
                log.info("dropped idle sdk profile %s", key)
                continue
            try:
                await self.refresh_profile(self._profile_from_entry(entry))
            except Exception as e:
                log.error("refresh failed for %s: %s", key, e)

    @staticmethod
    def seconds_until_refresh(hour_utc: int, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def daily_loop(self) -> None:
        while True:
            delay = self.seconds_until_refresh(self.cfg.sdk_refresh_hour_utc)
            log.info("next sdk refresh in %.0f s", delay)
            await asyncio.sleep(delay)
            try:
                await self.refresh_all()
            except Exception as e:
                log.error("daily refresh crashed: %s", e)

    def status(self) -> list[dict]:
        result = []
        for key, entry in self._registry.items():
            profile = self._profile_from_entry(entry)
            result.append(
                {
                    "key": key,
                    "index_url": entry["index_url"],
                    "channel": entry["channel"],
                    "target": entry["target"],
                    "deployed": profile.deployed,
                    "version": profile.version,
                }
            )
        return result
