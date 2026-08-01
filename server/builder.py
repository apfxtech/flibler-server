import asyncio
import logging
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .sdk import SdkProfile

log = logging.getLogger("flibler.builder")

MANIFEST_NAME = "application.fam"

class BuildError(Exception):
    pass

class BundleNotFoundError(BuildError):
    pass

class BundleDownloadError(BuildError):
    pass

class BundleTimeoutError(BuildError):
    pass

@dataclass
class BuiltFap:
    path: Path
    appid: str

def check_bundle_url(url: str, timeout: int = 15) -> None:
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": "qUnleashed-builder"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            pass
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            raise BundleNotFoundError(f"Bundle not found (HTTP {e.code})") from e
        if e.code in (405, 501):
            # host does not support HEAD; let the build attempt the real download
            return
        raise BundleDownloadError(f"Bundle host returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise BundleTimeoutError("Bundle host timed out") from e
        raise BundleDownloadError(f"Bundle host is unreachable: {e.reason}") from e
    except TimeoutError as e:
        raise BundleTimeoutError("Bundle host timed out") from e

def _download_bundle(url: str, dest: Path, max_bytes: int, timeout: int = 60) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "qUnleashed-builder"})
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            with open(dest, "wb") as out:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise BuildError("Bundle is too large")
                    out.write(chunk)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            raise BundleNotFoundError(f"Bundle not found (HTTP {e.code})") from e
        raise BundleDownloadError(f"Bundle host returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise BundleTimeoutError("Bundle download timed out") from e
        raise BundleDownloadError(f"Bundle download failed: {e.reason}") from e
    except TimeoutError as e:
        raise BundleTimeoutError("Bundle download timed out") from e
    if total == 0:
        raise BuildError("Bundle is empty")

def _safe_extract(zip_path: Path, dest: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            parts = Path(info.filename).parts
            if any(part in ("..", "") for part in parts) or Path(info.filename).is_absolute():
                continue
            archive.extract(info, dest)

def _find_app_dir(root: Path) -> Path:
    for candidate in (root / "code" / MANIFEST_NAME, root / MANIFEST_NAME):
        if candidate.is_file():
            return candidate.parent
    for manifest in sorted(root.rglob(MANIFEST_NAME)):
        return manifest.parent
    raise BuildError(f"No {MANIFEST_NAME} found in the bundle")


def _manifest_appid(app_dir: Path) -> str | None:
    try:
        source = (app_dir / MANIFEST_NAME).read_text(errors="replace")
    except OSError:
        return None
    match = re.search(r'appid\s*=\s*"([a-z0-9_]+)"', source)
    return match.group(1) if match else None


def _pick_fap(dist_dir: Path, appid: str | None) -> Path:
    if not dist_dir.is_dir():
        raise BuildError("Build produced no dist directory")
    faps = sorted(dist_dir.glob("*.fap"))
    if not faps:
        raise BuildError("Build produced no .fap artifact")
    if appid:
        for fap in faps:
            if fap.stem == appid:
                return fap
    return faps[0]

class BundleBuilder:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.work_root = cfg.data_dir / "work"
        self.work_root.mkdir(parents=True, exist_ok=True)

    def _env_for(self, profile: SdkProfile) -> dict:
        env = dict(os.environ)
        env["UFBT_HOME"] = str(profile.home)
        env["FBT_TOOLCHAIN_PATH"] = str(profile.home)
        env.pop("FBT_TOOLCHAIN_VERSION", None)
        return env

    async def build(
        self, job_id: str, bundle_url: str, profile: SdkProfile, log_path: Path
    ) -> BuiltFap:
        workdir = self.work_root / job_id
        shutil.rmtree(workdir, ignore_errors=True)
        workdir.mkdir(parents=True)
        try:
            bundle_zip = workdir / "bundle.zip"
            await asyncio.to_thread(
                _download_bundle,
                bundle_url,
                bundle_zip,
                self.cfg.max_bundle_mb * 1024 * 1024,
            )
            source_root = workdir / "src"
            await asyncio.to_thread(_safe_extract, bundle_zip, source_root)
            app_dir = _find_app_dir(source_root)
            appid = _manifest_appid(app_dir)
            await self._run_ufbt(app_dir, profile, log_path)
            fap = _pick_fap(app_dir / "dist", appid)
            result_path = workdir / fap.name
            shutil.copy2(fap, result_path)
            return BuiltFap(path=result_path, appid=appid or fap.stem)
        finally:
            shutil.rmtree(workdir / "src", ignore_errors=True)
            (workdir / "bundle.zip").unlink(missing_ok=True)

    async def _run_ufbt(self, app_dir: Path, profile: SdkProfile, log_path: Path) -> None:
        with open(log_path, "ab") as log_file:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "ufbt",
                cwd=str(app_dir),
                env=self._env_for(profile),
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=self.cfg.build_timeout_sec
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise BuildError(
                    f"Build timed out after {self.cfg.build_timeout_sec}s"
                )
        if process.returncode != 0:
            raise BuildError(f"ufbt failed with exit code {process.returncode}")

    def cleanup_job(self, job_id: str) -> None:
        shutil.rmtree(self.work_root / job_id, ignore_errors=True)
