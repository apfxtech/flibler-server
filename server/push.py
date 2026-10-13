import asyncio
import json
import logging
import re
import time

from .config import Config
from .db import Database, loads

log = logging.getLogger("catalog.push")

TOPIC_RE = re.compile(r"[^a-zA-Z0-9_.~%-]")
TOPIC_ALL = "catalog"


def topic_safe(value: str) -> str:
    return TOPIC_RE.sub("_", value)[:200]


def item_topic(item_id: str) -> str:
    return topic_safe(f"catalog_item_{item_id}")


def author_topic(author_id: str) -> str:
    return topic_safe(f"catalog_author_{author_id}")


class PushSender:
    """FCM through firebase-admin. Without a service account every message is
    logged instead of sent, so the rest of the server behaves the same."""

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self._app = None
        self.sent: list[dict] = []  # what a dry run would have sent (tests)
        if cfg.fcm_service_account:
            try:
                import firebase_admin
                from firebase_admin import credentials

                self._app = firebase_admin.initialize_app(credentials.Certificate(cfg.fcm_service_account))
            except Exception as e:
                log.warning("push disabled: %s", e)

    # ------------------------------------------------------------------- send

    def _send(self, condition: str, title: str, body: str, data: dict) -> None:
        record = {"condition": condition, "title": title, "body": body, "data": data}
        self.sent.append(record)
        if self._app is None:
            log.info("push (dry): %s | %s | %s", condition, title, body)
            return
        from firebase_admin import messaging

        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in data.items()},
            condition=condition,
        )
        messaging.send(message, app=self._app)

    # ------------------------------------------------------------------ outbox

    def _ready_rows(self) -> list:
        """Outbox rows whose version has a successful build (animations need
        none), oldest first."""
        return self.db.all(
            "SELECT o.* FROM push_outbox o WHERE o.sent_at IS NULL AND (o.family = 'animation' OR EXISTS ("
            "SELECT 1 FROM builds b JOIN versions v ON v.id = b.version_id WHERE v.item_id = o.item_id "
            "AND v.version = o.version AND b.result = 'ok')) ORDER BY o.created_at"
        )

    async def flush(self) -> int:
        rows = self._ready_rows()
        if not rows:
            return 0
        now = time.time()
        recent = int(self.db.scalar("SELECT COUNT(*) FROM push_outbox WHERE sent_at >= ?", (now - 86400,)))
        digest = recent + len(rows) > self.cfg.push_digest_threshold and len(rows) > 1
        if digest:
            new = sum(1 for r in rows if r["event"] == "new")
            updates = len(rows) - new
            self._send(
                f"'{TOPIC_ALL}' in topics",
                "qUnleashed catalog",
                f"{new} new, {updates} updated",
                {"type": "catalog", "event": "digest", "new": new, "updates": updates},
            )
        for row in rows:
            data = {
                "type": "catalog",
                "event": row["event"],
                "family": row["family"],
                "id": row["item_id"],
                "alias": row["alias"],
                "origin": row["origin"],
                "version": row["version"],
            }
            parts = [] if digest else [f"'{TOPIC_ALL}' in topics"]
            parts.append(f"'{item_topic(row['item_id'])}' in topics")
            for author in loads(row["authors"], [])[:3]:
                parts.append(f"'{author_topic(author['id'])}' in topics")
            title = "New in catalog" if row["event"] == "new" else "Update available"
            body = f"{row['name']} {row['version']}"
            try:
                self._send(" || ".join(parts), title, body, data)
            except Exception as e:
                log.warning("push for %s failed: %s", row["alias"], e)
                continue
            self.db.execute("UPDATE push_outbox SET sent_at = ? WHERE id = ?", (now, row["id"]))
        return len(rows)
