import logging
import shutil
import subprocess
from pathlib import Path

from .config import Config, FAMILIES

log = logging.getLogger("catalog.archive")


class ArchiveError(Exception):
    pass


def copy_tree(src: Path, dest: Path, *, skip_hidden: bool = True, skip: tuple[str, ...] = ()) -> None:
    """Copies a third-party tree without following or keeping symlinks: a
    link may be dangling, or point at a file of this server. Hidden entries
    and the names in `skip` are left out."""

    def ignore(directory: str, names: list[str]) -> set[str]:
        out = set()
        for name in names:
            if (skip_hidden and name.startswith(".")) or name in skip:
                out.add(name)
            elif (Path(directory) / name).is_symlink():
                out.add(name)
        return out

    shutil.copytree(src, dest, symlinks=False, ignore=ignore)


def safe_segment(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "_-." else "_" for ch in value)
    cleaned = cleaned.strip("._")
    if not cleaned or cleaned in (".", ".."):
        raise ArchiveError(f"Unusable path segment: {value!r}")
    return cleaned


def item_path(source: str, category: str, appid: str) -> str:
    parts = [safe_segment(source)]
    for segment in category.split("/"):
        segment = segment.strip()
        if segment:
            parts.append(safe_segment(segment))
    parts.append(safe_segment(appid))
    return "/".join(parts)


def tag_name(source: str, appid: str, version: str) -> str:
    return f"{safe_segment(source)}/{safe_segment(appid)}/{safe_segment(version)}"


class Archive:
    """One git repository per family. Every published version is a commit into
    the item's folder plus a tag; bundles are exported from the tree by tag."""

    def __init__(self, cfg: Config, family: str):
        if family not in FAMILIES:
            raise ArchiveError(f"Unknown family: {family}")
        self.cfg = cfg
        self.family = family
        self.root = cfg.archive_root / family

    def _git(self, *args: str, check: bool = True, capture: bool = True) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=str(self.root),
            text=True,
            capture_output=capture,
        )
        if check and result.returncode != 0:
            raise ArchiveError(
                f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
            )
        return result.stdout if capture else ""

    def ensure(self) -> None:
        if (self.root / ".git").is_dir():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", self.cfg.archive_git_user)
        self._git("config", "user.email", self.cfg.archive_git_email)
        (self.root / "README.md").write_text(
            f"# qUnleashed catalog archive: {self.family}\n\n"
            "Layout: /<source>/<category>[/<subcategory>]/<appid>/\n"
            "Each published version is a commit and a tag <source>/<appid>/<version>.\n"
        )
        self._git("add", "README.md")
        self._git("commit", "-q", "-m", "Archive: init")

    def has_tag(self, tag: str) -> bool:
        return self._git("tag", "-l", tag).strip() == tag

    def put(self, rel_path: str, src_dir: Path, tag: str, message: str) -> str:
        """Replaces the item folder with src_dir, commits and tags it.
        Returns the commit sha. A tag that already exists is left alone."""
        self.ensure()
        if self.has_tag(tag):
            return self._git("rev-list", "-n", "1", tag).strip()
        dest = self.root / rel_path
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        copy_tree(src_dir, dest, skip_hidden=False, skip=(".git",))
        self._git("add", "-A", "--", rel_path)
        status = self._git("status", "--porcelain", "--", rel_path).strip()
        if status:
            self._git("commit", "-q", "-m", message)
        self._git("tag", tag)
        return self._git("rev-list", "-n", "1", tag).strip()

    def remove(self, rel_path: str, message: str) -> None:
        self.ensure()
        if not (self.root / rel_path).exists():
            return
        self._git("rm", "-r", "-q", "--", rel_path)
        self._git("commit", "-q", "-m", message)

    def export_zip(self, tag: str, rel_path: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._git("archive", "--format=zip", "-o", str(dest), f"{tag}:{rel_path}")
        return dest

    def export_tree(self, tag: str, rel_path: str, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(
            ["git", "archive", "--format=tar", f"{tag}:{rel_path}"],
            cwd=str(self.root),
            capture_output=True,
        )
        if archive.returncode != 0:
            raise ArchiveError(archive.stderr.decode(errors="replace").strip())
        extract = subprocess.run(
            ["tar", "-x", "-C", str(dest)], input=archive.stdout, capture_output=True
        )
        if extract.returncode != 0:
            raise ArchiveError(extract.stderr.decode(errors="replace").strip())
        return dest

    def list_files(self, tag: str, rel_path: str) -> list[str]:
        out = self._git("ls-tree", "-r", "--name-only", f"{tag}:{rel_path}")
        return [line for line in out.splitlines() if line]

    def read_file(self, tag: str, path: str) -> bytes:
        result = subprocess.run(
            ["git", "show", f"{tag}:{path}"], cwd=str(self.root), capture_output=True
        )
        if result.returncode != 0:
            raise ArchiveError(result.stderr.decode(errors="replace").strip())
        return result.stdout

    def mirror(self) -> bool:
        """Pushes everything to the mirror. Failure is logged and reported, not
        raised: the next publish pushes what this one could not."""
        if not self.cfg.archive_mirror_base:
            return False
        url = f"{self.cfg.archive_mirror_base}/{self.family}.git"
        try:
            self._git("push", "--mirror", "-q", url)
            return True
        except ArchiveError as e:
            log.warning("mirror push for %s failed: %s", self.family, e)
            return False
