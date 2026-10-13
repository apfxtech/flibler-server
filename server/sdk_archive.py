import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import Database

log = logging.getLogger("catalog.sdk")

API_RE = re.compile(r"^\d{1,5}\.\d{1,5}$")

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


CHANNEL_INDEX_IDS = {"release": "release", "dev": "development"}


@dataclass(frozen=True)
class SdkProfile:
    target: str
    api: str
    version: str
    zip_path: Path
    home: Path
    channel: str = "release"

    @property
    def state_file(self) -> Path:
        return self.home / "current" / "ufbt_state.json"

    @property
    def deployed(self) -> bool:
        return self.state_file.is_file()


def api_from_sdk_zip(zip_path: Path) -> str:
    """The SDK zip carries targets/<t>/api_symbols.csv whose second line is
    `Version,+,<major.minor>,,`. That is the firmware API the SDK targets."""
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            if not name.endswith("api_symbols.csv"):
                continue
            with archive.open(name) as handle:
                for raw in handle:
                    line = raw.decode(errors="replace").strip()
                    if line.startswith("Version,"):
                        parts = line.split(",")
                        if len(parts) >= 3 and API_RE.match(parts[2]):
                            return parts[2]
                        break
    raise SdkError(f"No API version in {zip_path.name}")


def _fetch_json(url: str, timeout: int = 30) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "uFBT SDKLoader/0.2"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def _download(url: str, dest: Path, timeout: int = 300) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "uFBT SDKLoader/0.2"})
    with urllib.request.urlopen(request, timeout=timeout) as response, open(tmp, "wb") as out:
        shutil.copyfileobj(response, out, length=1 << 16)
    tmp.replace(dest)


class SdkArchive:
    """Every SDK version ever seen is kept as its zip; each version deploys into
    its own ufbt home, so a new release never replaces an older one."""

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.zips = cfg.sdk_root / "zips"
        self.homes = cfg.sdk_root / "homes"
        self.zips.mkdir(parents=True, exist_ok=True)
        self.homes.mkdir(parents=True, exist_ok=True)
        self._deploy_locks: dict[str, asyncio.Lock] = {}

    # ----------------------------------------------------------------- registry

    def _profile(self, row) -> SdkProfile:
        return SdkProfile(
            target=row["target"],
            api=row["api"],
            version=row["version"],
            zip_path=Path(row["zip_path"]),
            home=self.homes / row["target"] / row["version"],
            channel=row["channel"],
        )

    def profile_for(self, target: str, api: str) -> SdkProfile | None:
        """The SDK to build for this API: the current release when it has
        that API, else the current dev, else the newest SDK ever seen with it."""
        for channel in ("release", "dev"):
            current = self.latest(target, channel)
            if current is not None and current.api == api:
                return current
        row = self.db.one(
            "SELECT * FROM sdks WHERE target = ? AND api = ? ORDER BY added_at DESC LIMIT 1",
            (target, api),
        )
        return None if row is None else self._profile(row)

    def profile_for_version(self, target: str, version: str) -> SdkProfile | None:
        row = self.db.one("SELECT * FROM sdks WHERE target = ? AND version = ?", (target, version))
        return None if row is None else self._profile(row)

    def latest(self, target: str, channel: str | None = "release") -> SdkProfile | None:
        """The current SDK of a channel (release by default); channel=None
        is the newest of any channel."""
        if channel is None:
            row = self.db.one(
                "SELECT * FROM sdks WHERE target = ? ORDER BY added_at DESC LIMIT 1", (target,)
            )
        else:
            row = self.db.one(
                "SELECT * FROM sdks WHERE target = ? AND channel = ? ORDER BY added_at DESC LIMIT 1",
                (target, channel),
            )
        return None if row is None else self._profile(row)

    def current(self, target: str) -> list[SdkProfile]:
        """The SDK the server keeps per channel: release first, then dev."""
        out = []
        for channel in self.cfg.sdk_channels:
            profile = self.latest(target, channel)
            if profile is not None:
                out.append(profile)
        return out

    def held_versions(self) -> set[tuple[str, str]]:
        """(target, sdk_version) whose builds never expire: the current release."""
        out = set()
        for target in self.targets():
            release = self.latest(target, "release")
            if release is not None:
                out.add((target, release.version))
        return out

    def known_apis(self, target: str) -> list[str]:
        rows = self.db.all(
            "SELECT api FROM sdks WHERE target = ? GROUP BY api ORDER BY MAX(added_at) DESC",
            (target,),
        )
        return [r["api"] for r in rows]

    def active_apis(self, target: str) -> list[str]:
        cutoff = time.time() - self.cfg.sdk_active_days * 86400
        rows = self.db.all(
            "SELECT api FROM sdks WHERE target = ? AND last_used >= ? "
            "GROUP BY api ORDER BY MAX(added_at) DESC",
            (target, cutoff),
        )
        return [r["api"] for r in rows]

    def targets(self) -> list[str]:
        return [r["target"] for r in self.db.all("SELECT DISTINCT target FROM sdks")]

    def touch(self, target: str, api: str) -> None:
        self.db.execute(
            "UPDATE sdks SET last_used = ? WHERE target = ? AND api = ?",
            (time.time(), target, api),
        )

    def status(self) -> list[dict]:
        out = []
        for row in self.db.all("SELECT * FROM sdks ORDER BY target, added_at DESC"):
            profile = self._profile(row)
            out.append(
                {
                    "target": row["target"],
                    "channel": row["channel"],
                    "version": row["version"],
                    "api": row["api"],
                    "deployed": profile.deployed,
                    "added_at": row["added_at"],
                    "last_used": row["last_used"],
                }
            )
        return out

    def add_zip(self, target: str, version: str, zip_path: Path, channel: str = "release") -> SdkProfile:
        """Registers an SDK zip that is already on disk (seeding old releases)."""
        api = api_from_sdk_zip(zip_path)
        dest = self.zips / target / f"{version}.zip"
        if zip_path.resolve() != dest.resolve():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(zip_path, dest)
        now = time.time()
        self.db.execute(
            "INSERT INTO sdks (target, version, channel, api, zip_path, added_at, last_used) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(target, version) DO UPDATE SET "
            "api = excluded.api, zip_path = excluded.zip_path, channel = excluded.channel",
            (target, version, channel, api, str(dest), now, now),
        )
        log.info("sdk %s %s (%s) registered, api %s", target, version, channel, api)
        return self.profile_for_version(target, version)

    # -------------------------------------------------------------------- index

    def _latest_in_channel(self, target: str, channel_name: str) -> tuple[str, str]:
        index = _fetch_json(self.cfg.sdk_index_url)
        wanted = CHANNEL_INDEX_IDS.get(channel_name, channel_name)
        for channel in index.get("channels", []):
            if channel.get("id") != wanted:
                continue
            versions = channel.get("versions") or []
            if not versions:
                raise SdkError(f"Empty {wanted} channel")
            version = versions[0]["version"]
            for file in versions[0].get("files", []):
                if file.get("type") == "sdk_zip" and file.get("target") == target:
                    return version, file["url"]
            raise SdkError(f"No sdk_zip for {target} in {version}")
        raise SdkError(f"No {wanted} channel in index")

    async def check_index(self, target: str, channel: str = "release") -> SdkProfile | None:
        """Reads the index; downloads and registers the channel's SDK when it
        is new. Returns the new profile, or None when nothing changed."""
        version, url = await asyncio.to_thread(self._latest_in_channel, target, channel)
        known = self.profile_for_version(target, version)
        if known is not None:
            return None
        dest = self.zips / target / f"{version}.zip"
        log.info("downloading sdk %s %s (%s)", target, version, channel)
        await asyncio.to_thread(_download, url, dest)
        return await asyncio.to_thread(self.add_zip, target, version, dest, channel)

    # ------------------------------------------------------------------- deploy

    def _env_for(self, profile: SdkProfile) -> dict:
        env = dict(os.environ)
        env["UFBT_HOME"] = str(profile.home)
        env["UFBT_STATE_DIR"] = str(profile.home)
        env["FBT_TOOLCHAIN_PATH"] = str(profile.home)
        env.pop("FBT_TOOLCHAIN_VERSION", None)
        return env

    async def _run_logged(self, args: list[str], env: dict, cwd: Path, label: str) -> None:
        log.info("%s: %s", label, " ".join(args))
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(), timeout=self.cfg.sdk_deploy_timeout_sec
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise SdkError(f"{label} timed out after {self.cfg.sdk_deploy_timeout_sec}s")
        tail = output.decode(errors="replace")[-4000:]
        if process.returncode != 0:
            raise SdkError(f"{label} failed (exit {process.returncode}):\n{tail}")

    async def deploy(self, profile: SdkProfile) -> None:
        profile.home.mkdir(parents=True, exist_ok=True)
        args = [
            sys.executable,
            "-m",
            "ufbt",
            "update",
            "--local",
            str(profile.zip_path),
            "--hw-target",
            profile.target,
        ]
        await self._run_logged(args, self._env_for(profile), profile.home, f"sdk deploy {profile.version}")
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
                f"sdk warmup {profile.version}",
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def ensure_deployed(self, profile: SdkProfile) -> None:
        if profile.deployed:
            return
        lock = self._deploy_locks.setdefault(f"{profile.target}/{profile.version}", asyncio.Lock())
        async with lock:
            if not profile.deployed:
                await self.deploy(profile)
