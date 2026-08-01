import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

META_NAME = "meta.json"

@dataclass
class CacheEntry:
    key: str
    directory: Path
    meta: dict

    @property
    def fap_path(self) -> Path:
        return self.directory / self.meta["fap_name"]

    @property
    def sdk_version(self) -> str | None:
        return self.meta.get("sdk_version")

class BuildCache:
    def __init__(self, root: Path, ttl_days: int):
        self.root = root
        self.ttl_sec = ttl_days * 86400
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_for(payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _entry_dir(self, key: str) -> Path:
        return self.root / key

    def get(self, key: str) -> CacheEntry | None:
        directory = self._entry_dir(key)
        meta_file = directory / META_NAME
        if not meta_file.is_file():
            return None
        try:
            meta = json.loads(meta_file.read_text())
        except (OSError, json.JSONDecodeError):
            shutil.rmtree(directory, ignore_errors=True)
            return None
        if time.time() - meta.get("created_at", 0) > self.ttl_sec:
            shutil.rmtree(directory, ignore_errors=True)
            return None
        entry = CacheEntry(key=key, directory=directory, meta=meta)
        if not entry.fap_path.is_file():
            shutil.rmtree(directory, ignore_errors=True)
            return None
        return entry

    def put(self, key: str, fap_file: Path, meta: dict) -> CacheEntry:
        directory = self._entry_dir(key)
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        fap_name = fap_file.name
        shutil.copy2(fap_file, directory / fap_name)
        full_meta = {
            **meta,
            "fap_name": fap_name,
            "fap_size": (directory / fap_name).stat().st_size,
            "created_at": time.time(),
        }
        (directory / META_NAME).write_text(json.dumps(full_meta, indent=2))
        return CacheEntry(key=key, directory=directory, meta=full_meta)

    def cleanup(self) -> int:
        removed = 0
        for directory in self.root.iterdir():
            if not directory.is_dir():
                continue
            if self.get(directory.name) is None:
                removed += 1
        return removed
