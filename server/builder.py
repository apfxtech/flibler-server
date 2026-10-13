import asyncio
import hashlib
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
from urllib.parse import urlparse

from .config import Config
from .sdk_archive import SdkProfile

log = logging.getLogger("catalog.builder")

MANIFEST_NAME = "application.fam"
ARTIFACT_EXT = {"application": ".fap", "menustyle": ".fal"}


class BuildError(Exception):
    pass


class BundleNotFoundError(BuildError):
    pass


class BundleDownloadError(BuildError):
    pass


class BundleTimeoutError(BuildError):
    pass


class BuildCancelled(BuildError):
    pass


@dataclass
class BuiltArtifact:
    path: Path
    appid: str
    sha256: str
    size: int


class _HostCheckingRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: set[str]):
        super().__init__()
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname not in self.hosts:
            raise BundleDownloadError(f"Redirect to a host outside the allow list: {parsed.hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(hosts: set[str]) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_HostCheckingRedirect(hosts))


def check_bundle_url(url: str, hosts: set[str], timeout: int = 15) -> None:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "qUnleashed-builder"})
    try:
        with _opener(hosts).open(request, timeout=timeout):
            pass
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            raise BundleNotFoundError(f"Bundle not found (HTTP {e.code})") from e
        if e.code in (405, 501):
            return
        raise BundleDownloadError(f"Bundle host returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise BundleTimeoutError("Bundle host timed out") from e
        raise BundleDownloadError(f"Bundle host is unreachable: {e.reason}") from e
    except TimeoutError as e:
        raise BundleTimeoutError("Bundle host timed out") from e


def download_bundle(url: str, dest: Path, hosts: set[str], max_bytes: int, timeout: int = 60) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "qUnleashed-builder"})
    total = 0
    try:
        with _opener(hosts).open(request, timeout=timeout) as response, open(dest, "wb") as out:
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


def safe_extract(zip_path: Path, dest: Path, max_bytes: int) -> None:
    """Rejects path traversal and bounds the unpacked size: the declared sizes
    are checked first, and the real bytes written are counted as well, since a
    crafted entry can lie about its size."""
    total_declared = 0
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            total_declared += info.file_size
            if total_declared > max_bytes:
                raise BuildError("Bundle unpacks to more than the allowed size")
        written = 0
        for info in archive.infolist():
            name = info.filename
            parts = Path(name).parts
            if not parts or any(part in ("..", "") for part in parts) or Path(name).is_absolute():
                continue
            if name.endswith("/"):
                (dest / name).mkdir(parents=True, exist_ok=True)
                continue
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise BuildError("Bundle unpacks to more than the allowed size")
                    out.write(chunk)


def find_app_dir(root: Path) -> Path:
    for candidate in (root / "code" / MANIFEST_NAME, root / MANIFEST_NAME):
        if candidate.is_file():
            return candidate.parent
    for manifest in sorted(root.rglob(MANIFEST_NAME)):
        return manifest.parent
    raise BuildError(f"No {MANIFEST_NAME} found in the bundle")


_APP_RE = re.compile(r"App\s*\((.*?)\)(?=\s*(?:App\s*\(|\Z))", re.S)
_FIELD_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def manifest_apps(app_dir: Path) -> list[dict]:
    """Every App(...) block of application.fam as a dict of its string fields.
    Enough to pick the external app or plugin the bundle is about."""
    try:
        source = (app_dir / MANIFEST_NAME).read_text(errors="replace")
    except OSError:
        return []
    out = []
    for block in _APP_RE.findall(source):
        fields = dict(_FIELD_RE.findall(block))
        kind = re.search(r"apptype\s*=\s*FlipperAppType\.(\w+)", block)
        fields["apptype"] = kind.group(1) if kind else ""
        out.append(fields)
    return out


def pick_appid(app_dir: Path, family: str, wanted: str | None) -> str | None:
    apps = manifest_apps(app_dir)
    if wanted and any(a.get("appid") == wanted for a in apps):
        return wanted
    want_type = "PLUGIN" if family == "menustyle" else "EXTERNAL"
    for app in apps:
        if app.get("apptype") == want_type and app.get("appid"):
            return app["appid"]
    return wanted or (apps[0].get("appid") if apps else None)


def pick_artifact(dist_dir: Path, family: str, appid: str | None) -> Path:
    ext = ARTIFACT_EXT.get(family, ".fap")
    if not dist_dir.is_dir():
        raise BuildError("Build produced no dist directory")
    found = sorted(dist_dir.rglob(f"*{ext}"))
    if not found:
        raise BuildError(f"Build produced no {ext} artifact")
    if appid:
        for path in found:
            if path.stem == appid:
                return path
    if len(found) == 1:
        return found[0]
    raise BuildError(f"Build produced several {ext} artifacts and none is {appid}")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Builder:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.work_root = cfg.work_root
        self.work_root.mkdir(parents=True, exist_ok=True)
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    def _env_for(self, profile: SdkProfile) -> dict:
        env = dict(os.environ)
        env["UFBT_HOME"] = str(profile.home)
        env["FBT_TOOLCHAIN_PATH"] = str(profile.home)
        env.pop("FBT_TOOLCHAIN_VERSION", None)
        return env

    def workdir(self, job_id: str) -> Path:
        return self.work_root / job_id

    async def build_tree(
        self,
        job_id: str,
        source_root: Path,
        profile: SdkProfile,
        family: str,
        appid: str | None,
        log_path: Path,
    ) -> BuiltArtifact:
        app_dir = find_app_dir(source_root)
        chosen = pick_appid(app_dir, family, appid)
        await self._run_ufbt(job_id, app_dir, profile, log_path)
        artifact = pick_artifact(app_dir / "dist", family, chosen)
        result = self.workdir(job_id) / artifact.name
        shutil.copy2(artifact, result)
        return BuiltArtifact(
            path=result,
            appid=chosen or artifact.stem,
            sha256=sha256_of(result),
            size=result.stat().st_size,
        )

    async def build_url(
        self,
        job_id: str,
        bundle_url: str,
        profile: SdkProfile,
        family: str,
        appid: str | None,
        log_path: Path,
    ) -> BuiltArtifact:
        workdir = self.workdir(job_id)
        shutil.rmtree(workdir, ignore_errors=True)
        workdir.mkdir(parents=True)
        bundle_zip = workdir / "bundle.zip"
        await asyncio.to_thread(
            download_bundle,
            bundle_url,
            bundle_zip,
            self.cfg.bundle_hosts(),
            self.cfg.max_bundle_mb * 1024 * 1024,
        )
        source_root = workdir / "src"
        await asyncio.to_thread(safe_extract, bundle_zip, source_root, self.cfg.max_unpack_mb * 1024 * 1024)
        return await self.build_tree(job_id, source_root, profile, family, appid, log_path)

    async def _run_ufbt(self, job_id: str, app_dir: Path, profile: SdkProfile, log_path: Path) -> None:
        args = [*self.cfg.build_command_prefix, sys.executable, "-m", "ufbt"]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as log_file:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(app_dir),
                env=self._env_for(profile),
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._processes[job_id] = process
            try:
                await asyncio.wait_for(process.wait(), timeout=self.cfg.build_timeout_sec)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise BuildError(f"Build timed out after {self.cfg.build_timeout_sec}s")
            finally:
                self._processes.pop(job_id, None)
        if process.returncode == -9:
            raise BuildCancelled("Build cancelled")
        if process.returncode != 0:
            raise BuildError(f"ufbt failed with exit code {process.returncode}")

    def kill(self, job_id: str) -> bool:
        process = self._processes.get(job_id)
        if process is None or process.returncode is not None:
            return False
        process.kill()
        return True

    def cleanup_job(self, job_id: str) -> None:
        shutil.rmtree(self.workdir(job_id), ignore_errors=True)

    def scrub(self, text: str | None) -> str | None:
        if text is None:
            return None
        for secret_path in (str(self.cfg.data_dir), str(Path.home())):
            if secret_path and secret_path != "/":
                text = text.replace(secret_path, "<server>")
        return text
