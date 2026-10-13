import hashlib
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .archive import Archive, item_path, tag_name
from .config import Config
from .db import Database, dumps, loads

log = logging.getLogger("catalog.core")

# The eleven OFW categories with the ids the official catalogue uses, so the
# app's bundled icons keep matching. Order is priority.
APP_CATEGORIES = [
    ("64971d0f6617ba37a4bc79b3", "Sub-GHz", "A5F4BE", "subghz"),
    ("64971d10577d519190ede5c2", "RFID", "FFF493", "rfid"),
    ("64971d10be1a76c06747de26", "NFC", "98CEFE", "nfc"),
    ("64971d106617ba37a4bc79b6", "Infrared", "FE938C", "infrared"),
    ("64971d106617ba37a4bc79b9", "GPIO", "A7F2EA", "gpio"),
    ("64971d11be1a76c06747de29", "iButton", "E1BBA6", "ibutton"),
    ("64971d11be1a76c06747de2c", "USB", "FFBEE9", "usb"),
    ("64971d11be1a76c06747de2f", "Games", "FFC486", "games"),
    ("64971d116617ba37a4bc79bc", "Media", "DFB5FF", "media"),
    ("64971d11577d519190ede5c5", "Tools", "DFF159", "tools"),
    ("64a69817effe1f448a4053b4", "Bluetooth", "8BACFF", "bluetooth"),
]

MENUSTYLE_CATEGORIES = [
    ("layouts", "Layouts", "A7F2EA", "layouts"),
    ("theme", "Themes", "FFC486", "theme"),
    ("functional", "Functional", "DFF159", "functional"),
    ("animated", "Animated", "DFB5FF", "animated"),
]

ANIMATION_CATEGORIES = [
    ("ofw", "OFW", "FF8200"),
    ("unleashed", "Unleashed", "CC241D"),
    ("momentum", "Momentum", "A97BFF"),
    ("talking-sasquach", "Talking-Sasquach", "8FD694"),
    ("kuronons", "Kuronons", "98CEFE"),
    ("stopoxy", "Stopoxy", "FFBEE9"),
    ("doobthegoober", "DoobTheGoober", "DFF159"),
]
DEFAULT_CATEGORY_COLOR = "BDBDBD"

# Source priority: lower wins (section 4.5).
ORIGIN_PRIORITY = {"unleashed": 1, "ofw": 2, "atp": 3}
ORIGIN_ID_PREFIX = {"unleashed": "unlshd", "ofw": "ofw", "atp": "atp"}
FAMILY_ID_PREFIX = {"menustyle": "style", "animation": "anim"}

AUTHOR_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9_.-]*)")
APPID_RE = re.compile(r"^[a-z0-9_]+$")
ANIMATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
VERSION_RE = re.compile(r"^\d+(\.\d+)*$")


class PublishError(Exception):
    pass


def parse_authors(author: str) -> list[dict]:
    seen: list[dict] = []
    for name in AUTHOR_RE.findall(author or ""):
        entry = {"id": name.lower(), "name": name}
        if entry not in seen:
            seen.append(entry)
    return seen


def version_key(version: str) -> tuple:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)


@dataclass
class Manifest:
    """What every source hands to publish(): the catalog manifest after it was
    merged with application.fam, plus where the files are."""

    family: str
    source: str
    appid: str
    name: str
    version: str
    category: str
    short_description: str = ""
    description: str = ""
    changelog: str = ""
    author: str = ""
    commit_sha: str = ""
    origin_url: str = ""
    subdir: str = ""
    icon: Path | None = None
    screenshots: list[Path] = field(default_factory=list)
    bundle_dir: Path | None = None
    has_sources: bool = True
    preferred_id: str | None = None
    links: dict = field(default_factory=dict)
    install_path: str | None = None
    content_hash: str = ""


@dataclass
class PublishResult:
    item_id: str
    version_id: str
    created_item: bool
    created_version: bool
    took_over: bool


class Catalog:
    def __init__(self, cfg: Config, db: Database, archives: dict[str, Archive]):
        self.cfg = cfg
        self.db = db
        self.archives = archives
        cfg.assets_root.mkdir(parents=True, exist_ok=True)
        self.seed_categories()

    # --------------------------------------------------------------- categories

    def seed_categories(self) -> None:
        with self.db.tx() as conn:
            for priority, (cid, name, color, icon) in enumerate(APP_CATEGORIES):
                conn.execute(
                    "INSERT INTO categories (family, id, name, parent_id, color, icon, priority) "
                    "VALUES ('application', ?, ?, NULL, ?, ?, ?) ON CONFLICT(family, id) DO UPDATE SET "
                    "name = excluded.name, color = excluded.color, icon = excluded.icon, priority = excluded.priority",
                    (cid, name, color, icon, priority),
                )
            for priority, (cid, name, color, icon) in enumerate(MENUSTYLE_CATEGORIES):
                conn.execute(
                    "INSERT INTO categories (family, id, name, parent_id, color, icon, priority) "
                    "VALUES ('menustyle', ?, ?, NULL, ?, ?, ?) ON CONFLICT(family, id) DO NOTHING",
                    (cid, name, color, icon, priority),
                )
            for priority, (cid, name, color) in enumerate(ANIMATION_CATEGORIES):
                conn.execute(
                    "INSERT INTO categories (family, id, name, parent_id, color, icon, priority) "
                    "VALUES ('animation', ?, ?, NULL, ?, NULL, ?) ON CONFLICT(family, id) DO UPDATE SET "
                    "name = excluded.name, color = excluded.color, priority = excluded.priority",
                    (cid, name, color, priority),
                )
                conn.execute(
                    "UPDATE categories SET color = ? WHERE family = 'animation' AND parent_id IS NOT NULL AND id LIKE ?",
                    (color, f"{name}/%"),
                )

    def category_by_name(self, family: str, name: str):
        return self.db.one(
            "SELECT * FROM categories WHERE family = ? AND parent_id IS NULL AND lower(name) = lower(?)",
            (family, name),
        )

    def resolve_category(self, family: str, path: str) -> tuple[str, str]:
        """`GPIO/ESP32` -> (category_id, install_path). Top level must exist
        for applications; subcategories are created on first sight with the
        path as their id. Returns ('', '') for an empty path."""
        parts = [p.strip() for p in path.split("/") if p.strip()]
        if not parts:
            return "", ""
        top = self.category_by_name(family, parts[0])
        if top is None:
            if family == "application":
                raise PublishError(f"Unknown category: {parts[0]}")
            cid = parts[0].lower()
            self.db.execute(
                "INSERT INTO categories (family, id, name, parent_id, color, icon, priority) "
                "VALUES (?, ?, ?, NULL, ?, NULL, 100) ON CONFLICT(family, id) DO NOTHING",
                (family, cid, parts[0], DEFAULT_CATEGORY_COLOR),
            )
            top = self.db.one("SELECT * FROM categories WHERE family = ? AND id = ?", (family, cid))
        install = top["name"]
        parent_id = top["id"]
        for sub in parts[1:]:
            install = f"{install}/{sub}"
            self.db.execute(
                "INSERT INTO categories (family, id, name, parent_id, color, icon, priority) "
                "VALUES (?, ?, ?, ?, ?, NULL, 100) ON CONFLICT(family, id) DO NOTHING",
                (family, install, sub, parent_id, top["color"]),
            )
            parent_id = install
        return parent_id, install

    def top_category(self, family: str, category_id: str) -> str:
        """The root of a category path, as v0 reports it."""
        current = category_id
        for _ in range(8):
            row = self.db.one("SELECT parent_id FROM categories WHERE family = ? AND id = ?", (family, current))
            if row is None or not row["parent_id"]:
                return current
            current = row["parent_id"]
        return current

    def category_subtree(self, family: str, category_id: str) -> list[str]:
        out = [category_id]
        frontier = [category_id]
        while frontier:
            rows = self.db.all(
                f"SELECT id FROM categories WHERE family = ? AND parent_id IN ({','.join('?' * len(frontier))})",
                (family, *frontier),
            )
            frontier = [r["id"] for r in rows]
            out.extend(frontier)
        return out

    # ------------------------------------------------------------------- assets

    def store_asset(self, path: Path, content_type: str = "image/png") -> str:
        data = path.read_bytes()
        asset_id = hashlib.sha256(data).hexdigest()[:24]
        dest = self.cfg.assets_root / f"{asset_id}{path.suffix.lower() or '.png'}"
        if not dest.exists():
            dest.write_bytes(data)
        self.db.execute(
            "INSERT INTO assets (id, path, content_type) VALUES (?, ?, ?) ON CONFLICT(id) DO NOTHING",
            (asset_id, str(dest), content_type),
        )
        return asset_id

    def asset(self, asset_id: str):
        return self.db.one("SELECT * FROM assets WHERE id = ?", (asset_id,))

    # ------------------------------------------------------------------- lookup

    def item(self, item_id: str):
        return self.db.one("SELECT * FROM items WHERE id = ?", (item_id,))

    def resolve_item(self, family: str, id_or_alias: str):
        """Current id, any former id, or alias within the family."""
        row = self.db.one("SELECT * FROM items WHERE id = ? AND family = ?", (id_or_alias, family))
        if row is not None:
            return row
        alt = self.db.one(
            "SELECT i.* FROM item_ids a JOIN items i ON i.id = a.item_id WHERE a.alt_id = ? AND i.family = ?",
            (id_or_alias, family),
        )
        if alt is not None:
            return alt
        return self.db.one("SELECT * FROM items WHERE family = ? AND alias = ?", (family, id_or_alias))

    def version(self, version_id: str):
        return self.db.one("SELECT * FROM versions WHERE id = ?", (version_id,))

    def versions_of(self, item_id: str):
        rows = self.db.all("SELECT * FROM versions WHERE item_id = ?", (item_id,))
        return sorted(rows, key=lambda r: (version_key(r["version"]), r["created_at"]), reverse=True)

    # ------------------------------------------------------------------ publish

    def _new_item_id(self, manifest: Manifest) -> str:
        if manifest.preferred_id:
            return manifest.preferred_id
        prefix = FAMILY_ID_PREFIX.get(manifest.family) or ORIGIN_ID_PREFIX.get(manifest.source, manifest.source)
        return f"{prefix}:{manifest.appid}"

    def publish(self, manifest: Manifest, initial_import: bool = False) -> PublishResult | None:
        """Registers a version. Returns None when a higher-priority source
        already owns this appid. Idempotent for an existing version."""
        appid_rule = ANIMATION_ID_RE if manifest.family == "animation" else APPID_RE
        if not appid_rule.match(manifest.appid):
            raise PublishError(f"Bad appid: {manifest.appid!r}")
        if manifest.family != "animation" and not VERSION_RE.match(manifest.version):
            raise PublishError(f"Bad version: {manifest.version!r}")
        if manifest.bundle_dir is None or not manifest.bundle_dir.is_dir():
            raise PublishError("bundle_dir is missing")

        family, source, appid = manifest.family, manifest.source, manifest.appid
        now = time.time()
        existing = self.db.one("SELECT * FROM items WHERE family = ? AND appid = ?", (family, appid))
        took_over = False
        if existing is not None:
            have = ORIGIN_PRIORITY.get(existing["origin"], 99)
            want = ORIGIN_PRIORITY.get(source, 99)
            if want > have:
                log.info("%s/%s stays with %s, %s skipped", family, appid, existing["origin"], source)
                return None
            took_over = want < have
            item_id = existing["id"]
        else:
            item_id = self._new_item_id(manifest)

        category_id, install_from_category = self.resolve_category(family, manifest.category)
        install_path = manifest.install_path if manifest.install_path is not None else install_from_category
        rel_path = item_path(source, manifest.category or "misc", appid) if family != "animation" or manifest.category else item_path(source, "", appid)
        tag = tag_name(source, appid, manifest.version)
        version_id = f"{item_id}@{manifest.version}"

        authors = parse_authors(manifest.author)
        links = {
            "source_code": {"uri": manifest.origin_url} if manifest.origin_url else None,
            **manifest.links,
        }
        if existing is None:
            self.db.execute(
                "INSERT INTO items (id, family, appid, origin, alias, name, category_id, author, authors, "
                "short_description, install_path, links, current_version_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (item_id, family, appid, source, appid, manifest.name, category_id, manifest.author,
                 dumps(authors), manifest.short_description, install_path, dumps(links), now, now),
            )

        version_row = self.db.one("SELECT * FROM versions WHERE id = ?", (version_id,))
        created_version = version_row is None
        if created_version:
            self.archives[family].put(rel_path, manifest.bundle_dir, tag, f"{appid} {manifest.version} ({source})")
            icon_asset = self.store_asset(manifest.icon) if manifest.icon and manifest.icon.is_file() else None
            shots = [self.store_asset(p) for p in manifest.screenshots if p.is_file()]
            self.db.execute(
                "INSERT INTO versions (id, item_id, version, name, short_description, description, changelog, "
                "icon_asset, screenshots, install_path, tag, archive_path, commit_sha, bundle_sha256, has_sources, "
                "source_origin, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (version_id, item_id, manifest.version, manifest.name, manifest.short_description,
                 manifest.description, manifest.changelog, icon_asset, dumps(shots), install_path, tag, rel_path,
                 manifest.commit_sha, manifest.content_hash, int(manifest.has_sources), source, now),
            )

        if existing is not None and existing["current_version_id"] and not took_over:
            current = self.versions_of(item_id)[0]["id"]
        else:
            current = version_id
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE items SET origin = ?, name = ?, category_id = ?, author = ?, authors = ?, "
                "short_description = ?, install_path = ?, links = ?, current_version_id = ?, "
                "updated_at = CASE WHEN ? THEN ? ELSE updated_at END, hidden = 0 WHERE id = ?",
                (source, manifest.name, category_id, manifest.author, dumps(authors),
                 manifest.short_description, install_path, dumps(links), current,
                 int(created_version or took_over or existing is None), now, item_id),
            )
            conn.execute("INSERT OR IGNORE INTO item_ids (alt_id, item_id) VALUES (?, ?)", (item_id, item_id))
            prefix = FAMILY_ID_PREFIX.get(family) or ORIGIN_ID_PREFIX.get(source, source)
            conn.execute("INSERT OR IGNORE INTO item_ids (alt_id, item_id) VALUES (?, ?)", (f"{prefix}:{appid}", item_id))
            if manifest.preferred_id:
                conn.execute("INSERT OR IGNORE INTO item_ids (alt_id, item_id) VALUES (?, ?)", (manifest.preferred_id, item_id))
            if created_version and not initial_import:
                conn.execute(
                    "INSERT INTO push_outbox (created_at, event, family, item_id, alias, origin, version, name, authors) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (now, "new" if existing is None else "update", family, item_id, appid, source,
                     manifest.version, manifest.name, dumps(authors)),
                )
        if took_over:
            log.info("%s/%s taken over by %s from %s", family, appid, source, existing["origin"])
        return PublishResult(item_id, version_id, existing is None, created_version, took_over)

    def hide(self, family: str, appid: str, source: str) -> bool:
        """A manifest removed from its source: hidden when that source owned
        the item, untouched when a higher-priority source does."""
        row = self.db.one("SELECT * FROM items WHERE family = ? AND appid = ?", (family, appid))
        if row is None or row["origin"] != source:
            return False
        self.db.execute("UPDATE items SET hidden = 1, updated_at = ? WHERE id = ?", (time.time(), row["id"]))
        return True

    # ------------------------------------------------------------------- bundle

    def bundle_zip(self, version_row) -> Path | None:
        """The bundle for a version, exported from the archive on first use."""
        if not version_row["has_sources"]:
            return None
        family = self.db.scalar("SELECT family FROM items WHERE id = ?", (version_row["item_id"],))
        dest = self.cfg.bundles_root / family / f"{hashlib.sha256(version_row['id'].encode()).hexdigest()[:24]}.zip"
        if not dest.is_file():
            self.archives[family].export_zip(version_row["tag"], version_row["archive_path"], dest)
        return dest

    def cleanup_bundles(self, max_age_days: int = 7) -> int:
        removed = 0
        cutoff = time.time() - max_age_days * 86400
        if not self.cfg.bundles_root.exists():
            return 0
        for path in self.cfg.bundles_root.rglob("*.zip"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed
