import hashlib
import logging
import shutil
from pathlib import Path

from ..archive import copy_tree
from ..catalog import Manifest, PublishError
from .base import Source, SourceError

log = logging.getLogger("catalog.sources.animations")


def folder_hash(folder: Path, skip: tuple[str, ...] = (), extra: str = "") -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in folder.iterdir() if p.is_file() and p.name not in skip):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    digest.update(extra.encode())
    return digest.hexdigest()[:12]


class AnimationsSource(Source):
    """Any repository of dolphin folders: a folder with meta.txt is an
    animation, its parent folder (if not the root) is the category. There is
    no version to grow: the version is the content hash, so a changed folder
    is a new version and install equals update."""

    name = "animations"
    family = "animation"

    def import_changed(self, previous, head, initial) -> dict:
        report = {"published": 0, "unchanged": 0, "failed": 0, "errors": []}
        root = self.mirror.path
        for meta in sorted(root.rglob("meta.txt")):
            folder = meta.parent
            if ".git" in folder.parts:
                continue
            try:
                result = self._import_folder(folder, head, initial)
            except Exception as e:  # one failure costs one entry, not the pass
                report["failed"] += 1
                report["errors"].append(f"{folder.name}: {e}")
                continue
            if result is None:
                report["unchanged"] += 1
            elif result.created_version:
                report["published"] += 1
            else:
                report["unchanged"] += 1
        return report

    def _import_folder(self, folder: Path, head: str, initial: bool):
        rel_parent = folder.parent.relative_to(self.mirror.path).as_posix()
        category = "" if rel_parent in ("", ".") else rel_parent.split("/")[-1]
        version = folder_hash(folder)
        out_dir = self.work / "bundle" / folder.name
        shutil.rmtree(out_dir, ignore_errors=True)
        copy_tree(folder, out_dir)
        manifest = Manifest(
            family="animation",
            source="unleashed",
            appid=folder.name,
            name=folder.name,
            version=version,
            category=category,
            short_description="",
            description=(folder / "README.md").read_text(errors="replace") if (folder / "README.md").is_file() else "",
            commit_sha=head,
            origin_url=self.url,
            subdir=folder.relative_to(self.mirror.path).as_posix(),
            bundle_dir=out_dir,
            has_sources=False,
            install_path=f"/ext/dolphin/{folder.name}",
            content_hash=version,
        )
        try:
            return self.publish(manifest, initial)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
