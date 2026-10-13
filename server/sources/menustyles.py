import logging
import re
import shutil
from pathlib import Path

from ..bundle_tool import BundleError
from ..catalog import Manifest, PublishError
from .base import Source, SourceError

log = logging.getLogger("catalog.sources.menustyles")

INSTALL_PATH = "/ext/apps_data/loader/plugins"
LOADER_STUB = 'App(\n    appid="loader",\n    apptype=FlipperAppType.METAPACKAGE,\n)\n\n'
_APP_BLOCK = re.compile(r"App\s*\((.*?)\)(?=\s*(?:App\s*\(|\Z))", re.S)


def split_plugins(fam_text: str) -> list[tuple[str, dict]]:
    """Every PLUGIN App(...) block with its text and string fields."""
    out = []
    for match in _APP_BLOCK.finditer(fam_text):
        block = match.group(0)
        if "FlipperAppType.PLUGIN" not in block:
            continue
        fields = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', block))
        sources = re.search(r"sources\s*=\s*\[(.*?)\]", block, re.S)
        fields["_sources"] = re.findall(r'"([^"]+)"', sources.group(1)) if sources else []
        version = re.search(r"fap_version\s*=\s*(\(\s*(\d+)\s*,\s*(\d+)\s*\)|\"(\d+\.\d+)\")", block)
        fields["_version"] = (version.group(4) or f"{version.group(2)}.{version.group(3)}") if version else "1.0"
        out.append((block, fields))
    return out


class MenuStylesSource(Source):
    """all-menu-styles: one application.fam, one .fal per plugin block."""

    name = "menustyles"
    family = "menustyle"

    def import_changed(self, previous, head, initial) -> dict:
        fam = self.mirror.path / "application.fam"
        if not fam.is_file():
            raise SourceError("application.fam not found")
        report = {"published": 0, "unchanged": 0, "failed": 0, "errors": []}
        for block, fields in split_plugins(fam.read_text(errors="replace")):
            appid = fields.get("appid", "")
            try:
                result = self._import_style(block, fields, head, initial)
            except Exception as e:  # one failure costs one entry, not the pass
                report["failed"] += 1
                report["errors"].append(f"{appid}: {e}")
                log.warning("menustyle %s failed: %s", appid, e)
                continue
            if result is None:
                report["unchanged"] += 1
            elif result.created_version:
                report["published"] += 1
            else:
                report["unchanged"] += 1
        return report

    def _import_style(self, block: str, fields: dict, head: str, initial: bool):
        appid = fields.get("appid", "")
        sources = fields["_sources"]
        if not appid or not sources:
            raise BundleError("plugin without appid or sources")
        first = Path(sources[0])
        category = first.parts[1] if len(first.parts) > 2 and first.parts[0] == "styles" else "theme"
        out_dir = self.work / "bundle" / appid
        shutil.rmtree(out_dir, ignore_errors=True)
        code = out_dir / "code"
        code.mkdir(parents=True)
        copied = []
        for rel in sources:
            src = (self.mirror.path / rel).resolve()
            if not src.is_relative_to(self.mirror.path.resolve()) or not src.is_file():
                raise BundleError(f"source not found: {rel}")
            dest = code / Path(rel).name
            shutil.copy2(src, dest)
            copied.append(dest.name)
        block_text = re.sub(r"sources\s*=\s*\[.*?\]", "sources=[" + ", ".join(f'"{n}"' for n in copied) + "]", block, flags=re.S)
        (code / "application.fam").write_text(LOADER_STUB + block_text.strip() + "\n")
        (out_dir / "manifest.yml").write_text(
            f"id: {appid}\nname: {fields.get('name', appid)!r}\nversion: '{fields['_version']}'\ncategory: {category}\n"
        )
        manifest = Manifest(
            family="menustyle",
            source="unleashed",
            appid=appid,
            name=fields.get("name", appid),
            version=fields["_version"],
            category=category,
            short_description=fields.get("fap_description", ""),
            description=fields.get("fap_description", ""),
            author=fields.get("fap_author", ""),
            commit_sha=head,
            origin_url=self.url,
            subdir="",
            bundle_dir=out_dir,
            has_sources=True,
            install_path=INSTALL_PATH,
            links={"manifest_uri": f"{self.url.removesuffix('.git')}/tree/{head}"},
        )
        try:
            return self.publish(manifest, initial)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
