"""Builds the catalog bundle for one manifest.

Mirrors what flipper-application-catalog/tools/bundle.py produces: the merged
manifest, the source tree under code/, a 10x10 icon and 128x64 screenshots
under assets/. Differences, all deliberate: a nested category is allowed, lint
and the ufbt build are left to the build queue, and a source zip uploaded
without manifest.yml is taken as raw sources, with no screenshots required.
"""

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from PIL import Image

from .archive import copy_tree
from .builder import manifest_apps
from .catalog import Manifest, PublishError

log = logging.getLogger("catalog.bundle")

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SCREEN = (128, 64)
ICON = (10, 10)
BLACK_THRESHOLD = (15, 15, 15)


class BundleError(PublishError):
    pass


@dataclass
class SourceCheckout:
    path: Path
    commit_sha: str
    code_dir: Path


def git_output(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if result.returncode != 0:
        raise BundleError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()[:400]}")
    return result.stdout


def resolve_head(origin: str, branch: str | None = None) -> str:
    ref = branch or "HEAD"
    out = git_output(["ls-remote", origin, ref])
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if name.strip() == ref or ref == "HEAD":
            return sha.strip()
    raise BundleError(f"Cannot resolve {ref} of {origin}")


def checkout_source(origin: str, commit_sha: str | None, subdir: str, dest: Path, *, allow_unpinned: bool) -> SourceCheckout:
    if commit_sha and not SHA_RE.match(commit_sha):
        raise BundleError("commit_sha must be the full 40-character sha")
    if not commit_sha:
        if not allow_unpinned:
            raise BundleError("commit_sha is required for this origin")
        commit_sha = resolve_head(origin)
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    git_output(["init", "-q"], dest)
    git_output(["remote", "add", "origin", origin], dest)
    try:
        git_output(["fetch", "-q", "--depth", "1", "origin", commit_sha], dest)
    except BundleError:
        git_output(["fetch", "-q", "origin"], dest)
    git_output(["-c", "advice.detachedHead=false", "checkout", "-q", commit_sha], dest)
    git_output(["submodule", "update", "--init", "--recursive", "--depth", "1"], dest)
    code_dir = dest
    if subdir:
        code_dir = (dest / subdir).resolve()
        if not code_dir.is_relative_to(dest.resolve()):
            raise BundleError("subdir escapes the repository")
        if not code_dir.is_dir():
            raise BundleError(f"subdir not found: {subdir}")
    return SourceCheckout(path=dest, commit_sha=commit_sha, code_dir=code_dir)


def read_catalog_manifest(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise BundleError(f"manifest.yml unreadable: {e}")
    if not isinstance(data, dict):
        raise BundleError("manifest.yml is not a mapping")
    return data


def category_from_path(manifest_path: Path) -> tuple[str, str] | None:
    """applications/<Category>[/<Sub>]/<appid>/manifest.yml -> (category, appid)."""
    parts = manifest_path.parts
    if "applications" not in parts:
        return None
    idx = parts.index("applications")
    between = parts[idx + 1 : -1]
    if len(between) < 2:
        raise BundleError(f"manifest path too short: {manifest_path}")
    return "/".join(between[:-1]), between[-1]


def _fam_fields(code_dir: Path, wanted_id: str | None, family: str) -> dict:
    apps = manifest_apps(code_dir)
    if not apps:
        raise BundleError("application.fam has no App() entries")
    want = "PLUGIN" if family == "menustyle" else "EXTERNAL"
    candidates = [a for a in apps if a.get("apptype") == want]
    if wanted_id:
        candidates = [a for a in candidates if a.get("appid") == wanted_id] or candidates
    if not candidates:
        raise BundleError(f"No {want} app in application.fam")
    if len(candidates) > 1 and not wanted_id:
        raise BundleError("application.fam has several apps, set id in manifest.yml")
    return candidates[0]


FAP_VERSION_DEFAULT = "0.1"  # FlipperApplication.fap_version in fbt's appmanifest.py


def _fam_version(code_dir: Path, appid: str) -> str:
    """fap_version of one App() block, read the way fbt reads it: a string of
    dot-separated integers or a tuple of integers, "0.1" when absent; joined
    with dots as flipper-application-catalog's bundle.py does."""
    from .builder import _APP_RE

    text = (code_dir / "application.fam").read_text(errors="replace")
    for match in _APP_RE.finditer(text):
        block = match.group(0)
        if not re.search(rf'appid\s*=\s*"{re.escape(appid)}"', block):
            continue
        string = re.search(r'fap_version\s*=\s*"([^"]*)"', block)
        if string:
            parts = string.group(1).strip().split(".")
            if all(part.isdigit() for part in parts):
                return ".".join(str(int(part)) for part in parts)
            raise BundleError(f"Invalid fap_version {string.group(1)!r}")
        tuple_ = re.search(r"fap_version\s*=\s*[\(\[]([^\)\]]*)[\)\]]", block)
        if tuple_:
            parts = [part.strip() for part in tuple_.group(1).split(",") if part.strip()]
            if parts and all(part.isdigit() for part in parts):
                return ".".join(str(int(part)) for part in parts)
            raise BundleError(f"Invalid fap_version ({tuple_.group(1)})")
        return FAP_VERSION_DEFAULT
    return FAP_VERSION_DEFAULT


def _include(value: str, code_dir: Path) -> str:
    if isinstance(value, str) and value.startswith("@"):
        path = (code_dir / value[1:]).resolve()
        if not path.is_relative_to(code_dir.resolve()) or not path.is_file():
            raise BundleError(f"included file not found: {value}")
        return path.read_text(errors="replace")
    return value or ""


def _to_black_transparent(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    pixels = img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata()
    img.putdata([(0, 0, 0, 255) if p[:3] <= BLACK_THRESHOLD else (255, 255, 255, 0) for p in pixels])
    return img


def process_icon(src: Path, dest: Path) -> None:
    img = Image.open(src).convert("RGBA")
    if img.size != ICON:
        raise BundleError(f"icon {src.name} is {img.width}x{img.height}, expected 10x10")
    _to_black_transparent(img).save(dest, "PNG")


def process_screenshot(src: Path, dest: Path) -> None:
    img = Image.open(src).convert("RGBA")
    if img.width < img.height:
        raise BundleError(f"screenshot {src.name} is portrait")
    fx, fy = img.width // SCREEN[0], img.height // SCREEN[1]
    if fx != fy or fx not in (1, 4, 8) or (img.width // fx, img.height // fy) != SCREEN:
        raise BundleError(f"screenshot {src.name} is {img.width}x{img.height}, expected a 4x or 8x 128x64")
    if fx > 1:
        img = img.resize(SCREEN, resample=Image.Resampling.NEAREST)
    _to_black_transparent(img).save(dest, "PNG")


def build_bundle(
    *,
    family: str,
    source: str,
    catalog: dict,
    code_dir: Path,
    commit_sha: str,
    origin_url: str,
    subdir: str,
    out_dir: Path,
    path_category: str | None = None,
    path_appid: str | None = None,
    raw: bool = False,
    install_path: str | None = None,
) -> Manifest:
    """Merges manifest.yml with application.fam and lays the bundle out in
    out_dir. `raw` relaxes the metadata rules for unprepared sources."""
    fam = _fam_fields(code_dir, catalog.get("id") or path_appid, family)
    appid = catalog.get("id") or fam.get("appid", "")
    if path_appid and appid != path_appid:
        raise BundleError(f"appid {appid} does not match folder {path_appid}")
    name = catalog.get("name") or fam.get("name") or appid
    version = str(catalog.get("version") or _fam_version(code_dir, appid) or ("1.0" if raw else ""))
    if not version:
        raise BundleError("version is missing in both manifest.yml and application.fam")
    category = catalog.get("category") or fam.get("fap_category") or ""
    if path_category:
        top = path_category.split("/")[0]
        if category and category.lower() not in (path_category.lower(), top.lower()):
            raise BundleError(f"category {category} does not match folder {path_category}")
        category = path_category
    short = catalog.get("short_description") or fam.get("fap_description") or ""
    description = _include(catalog.get("description", ""), code_dir)
    changelog = _include(catalog.get("changelog", ""), code_dir)
    author = catalog.get("author") or fam.get("fap_author") or ""
    if not raw:
        for label, value in (("short_description", short), ("description", description), ("changelog", changelog)):
            if not value.strip():
                raise BundleError(f"{label} is empty")

    shutil.rmtree(out_dir, ignore_errors=True)
    (out_dir / "assets" / "screenshots").mkdir(parents=True)
    copy_tree(code_dir, out_dir / "code", skip=("dist",))

    icon_path = None
    icon_src = catalog.get("icon") or fam.get("fap_icon")
    if icon_src:
        candidate = (code_dir / icon_src).resolve()
        if candidate.is_relative_to(code_dir.resolve()) and candidate.is_file():
            icon_path = out_dir / "assets" / "icon.png"
            try:
                process_icon(candidate, icon_path)
            except BundleError:
                if not raw:
                    raise
                icon_path = None
    shots = []
    for index, rel in enumerate(catalog.get("screenshots") or []):
        candidate = (code_dir / str(rel)).resolve()
        if not candidate.is_relative_to(code_dir.resolve()) or not candidate.is_file():
            raise BundleError(f"screenshot not found: {rel}")
        dest = out_dir / "assets" / "screenshots" / f"{index}.png"
        process_screenshot(candidate, dest)
        shots.append(dest)
    if not shots and not raw:
        raise BundleError("at least one screenshot is required")

    merged = {
        "sourcecode": {"type": "git", "location": {"origin": origin_url, "commit_sha": commit_sha, **({"subdir": subdir} if subdir else {})}},
        "name": name,
        "id": appid,
        "author": author,
        "version": version,
        "category": category,
        "short_description": short,
        "description": description,
        "changelog": changelog,
        "icon": "assets/icon.png" if icon_path else "",
        "screenshots": [f"assets/screenshots/{i}.png" for i in range(len(shots))],
    }
    (out_dir / "manifest.yml").write_text(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True))

    return Manifest(
        family=family,
        source=source,
        appid=appid,
        name=name,
        version=version,
        category=category,
        short_description=short,
        description=description,
        changelog=changelog,
        author=author,
        commit_sha=commit_sha,
        origin_url=origin_url,
        subdir=subdir,
        icon=icon_path,
        screenshots=shots,
        bundle_dir=out_dir,
        has_sources=True,
        install_path=install_path,
    )


def manifest_from_bundle_zip(zip_path: Path, out_dir: Path, *, family: str, source: str) -> Manifest:
    """A bundle.zip as the catalog CI submits it (section 5)."""
    import zipfile

    from .builder import safe_extract

    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    safe_extract(zip_path, out_dir, 1 << 30)
    manifest_file = out_dir / "manifest.yml"
    if not manifest_file.is_file():
        raise BundleError("bundle has no manifest.yml")
    data = read_catalog_manifest(manifest_file)
    location = (data.get("sourcecode") or {}).get("location") or {}
    icon = out_dir / str(data.get("icon") or "assets/icon.png")
    shots = [out_dir / str(s) for s in data.get("screenshots") or []]
    code_dir = out_dir / "code"
    return Manifest(
        family=family,
        source=source,
        appid=str(data.get("id", "")),
        name=str(data.get("name", "")),
        version=str(data.get("version", "")),
        category=str(data.get("category", "")),
        short_description=str(data.get("short_description", "")),
        description=str(data.get("description", "")),
        changelog=str(data.get("changelog", "")),
        author=str(data.get("author", "")),
        commit_sha=str(location.get("commit_sha", "")),
        origin_url=str(location.get("origin", "")),
        subdir=str(location.get("subdir", "") or ""),
        icon=icon if icon.is_file() else None,
        screenshots=[s for s in shots if s.is_file()],
        bundle_dir=out_dir,
        has_sources=code_dir.is_dir(),
    )


def bundle_from_source_zip(
    zip_path: Path,
    work_dir: Path,
    *,
    family: str,
    source: str,
    category: str | None,
    commit_sha: str,
    origin_url: str,
    max_unpack_bytes: int,
) -> Manifest:
    """A zip of sources as a CI uploads it: application.fam at the root or
    under code/, optionally a manifest.yml next to it with description,
    changelog and screenshots. With manifest.yml the OFW rules apply; without
    it the zip is taken as raw sources."""
    from .builder import find_app_dir, safe_extract

    import zipfile

    shutil.rmtree(work_dir, ignore_errors=True)
    unpacked = work_dir / "src"
    unpacked.mkdir(parents=True)
    try:
        safe_extract(zip_path, unpacked, max_unpack_bytes)
        code_dir = find_app_dir(unpacked)
    except zipfile.BadZipFile:
        raise BundleError("not a zip file")
    except Exception as e:
        raise BundleError(str(e))
    catalog: dict = {}
    for candidate in (unpacked / "manifest.yml", code_dir / "manifest.yml"):
        if candidate.is_file():
            catalog = read_catalog_manifest(candidate)
            break
    raw = not catalog
    if category:
        catalog["category"] = category
    return build_bundle(
        family=family,
        source=source,
        catalog=catalog,
        code_dir=code_dir,
        commit_sha=commit_sha,
        origin_url=origin_url,
        subdir="",
        out_dir=work_dir / "bundle",
        raw=raw,
    )


DOLPHIN_KEYS = ("min_butthurt", "max_butthurt", "min_level", "max_level", "weight")
ANIMATION_BUNDLE_FILE_RE = re.compile(r"^(manifest\.yml|meta\.txt|frame_\d+\.bm)$")


def animation_from_bundle_zip(zip_path: Path, work_dir: Path, *, max_unpack_bytes: int) -> Manifest:
    """An animation bundle as the catalog CI submits it: manifest.yml, meta.txt
    and frame_*.bm side by side at the zip root, nothing else. The version is
    the hash of the animation files and of its dolphin manifest entry."""
    import zipfile

    from .builder import safe_extract
    from .sources.animations import folder_hash

    shutil.rmtree(work_dir, ignore_errors=True)
    out_dir = work_dir / "bundle"
    out_dir.mkdir(parents=True)
    try:
        safe_extract(zip_path, out_dir, max_unpack_bytes)
    except zipfile.BadZipFile:
        raise BundleError("not a zip file")
    names = sorted(p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file())
    unexpected = [n for n in names if not ANIMATION_BUNDLE_FILE_RE.match(n)]
    if unexpected:
        raise BundleError(f"unexpected files in the bundle: {', '.join(unexpected[:5])}")
    for required in ("manifest.yml", "meta.txt"):
        if required not in names:
            raise BundleError(f"bundle has no {required}")
    if not any(n.startswith("frame_") for n in names):
        raise BundleError("bundle has no frame_*.bm")
    data = read_catalog_manifest(out_dir / "manifest.yml")
    name = str(data.get("id", "")).strip()
    if not name:
        raise BundleError("manifest.yml has no id")
    dolphin = {}
    for key in DOLPHIN_KEYS:
        if key in data:
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BundleError(f"{key} must be a non-negative integer")
            dolphin[key] = value
    location = (data.get("sourcecode") or {}).get("location") or {}
    version = folder_hash(out_dir, skip=("manifest.yml",), extra=json.dumps(dolphin, sort_keys=True) if dolphin else "")
    return Manifest(
        family="animation",
        source="unleashed",
        appid=name,
        name=name,
        version=version,
        category=str(data.get("category", "") or ""),
        short_description=str(data.get("short_description", "") or ""),
        description=str(data.get("description", "") or ""),
        author=str(data.get("author", "") or ""),
        commit_sha=str(location.get("commit_sha", "") or ""),
        origin_url=str(location.get("origin", "") or ""),
        subdir=str(location.get("subdir", "") or ""),
        bundle_dir=out_dir,
        has_sources=False,
        install_path=f"/ext/dolphin/{name}",
        content_hash=version,
    )


def animation_from_zip(
    zip_path: Path,
    work_dir: Path,
    *,
    name: str | None,
    category: str | None,
    commit_sha: str,
    origin_url: str,
    max_unpack_bytes: int,
) -> Manifest:
    """A zip of one dolphin animation: a folder (or the zip root) with
    meta.txt and frame_*.bm. Its name is the folder name unless given; the
    version is the content hash, so a changed animation is a new version."""
    import zipfile

    from .archive import copy_tree
    from .builder import safe_extract
    from .sources.animations import folder_hash

    shutil.rmtree(work_dir, ignore_errors=True)
    unpacked = work_dir / "src"
    unpacked.mkdir(parents=True)
    try:
        safe_extract(zip_path, unpacked, max_unpack_bytes)
    except zipfile.BadZipFile:
        raise BundleError("not a zip file")
    metas = sorted(unpacked.rglob("meta.txt"))
    if not metas:
        raise BundleError("no meta.txt in the zip")
    if len(metas) > 1:
        raise BundleError("one animation per upload")
    folder = metas[0].parent
    anim_name = (name or "").strip() or (folder.name if folder != unpacked else "")
    if not anim_name:
        raise BundleError("name is required when meta.txt is at the zip root")
    out_dir = work_dir / "bundle"
    copy_tree(folder, out_dir)
    version = folder_hash(out_dir)
    return Manifest(
        family="animation",
        source="unleashed",
        appid=anim_name,
        name=anim_name,
        version=version,
        category=category or "",
        description=(out_dir / "README.md").read_text(errors="replace") if (out_dir / "README.md").is_file() else "",
        commit_sha=commit_sha,
        origin_url=origin_url,
        bundle_dir=out_dir,
        has_sources=False,
        install_path=f"/ext/dolphin/{anim_name}",
        content_hash=version,
    )
