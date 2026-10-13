import time

from .auth import client_hash
from .config import Config
from .db import Database

PERIODS = {"week": 7, "month": 30, "season": 90, "year": 365}


class Stats:
    """The download journal: every artifact handed out is one row. Nothing is
    reported by the client; popularity is counted from what passes through."""

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db

    def record(self, *, item_id: str, version_id: str, family: str, target: str, api: str, client_id: str = "") -> None:
        origin = self.db.scalar("SELECT origin FROM items WHERE id = ?", (item_id,)) or ""
        self.db.execute(
            "INSERT INTO downloads (ts, item_id, version_id, family, origin, target, api, client_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), item_id, version_id, family, origin, target, api,
             client_hash(client_id, self.cfg.shared_secret) if client_id else ""),
        )

    def for_item(self, item_id: str) -> dict:
        now = time.time()
        out = {"downloads": int(self.db.scalar("SELECT COUNT(*) FROM downloads WHERE item_id = ?", (item_id,)))}
        for name, days in PERIODS.items():
            out[f"downloads_{name}"] = int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM downloads WHERE item_id = ? AND ts >= ?", (item_id, now - days * 86400)
                )
            )
        out["unique_week"] = int(
            self.db.scalar(
                "SELECT COUNT(DISTINCT client_hash) FROM downloads WHERE item_id = ? AND ts >= ? AND client_hash != ''",
                (item_id, now - 7 * 86400),
            )
        )
        return out

    def counts(self, period: str | None) -> dict[str, int]:
        """item_id -> downloads within the period (or all time)."""
        if period and period in PERIODS:
            rows = self.db.all(
                "SELECT item_id, COUNT(*) AS n FROM downloads WHERE ts >= ? GROUP BY item_id",
                (time.time() - PERIODS[period] * 86400,),
            )
        else:
            rows = self.db.all("SELECT item_id, COUNT(*) AS n FROM downloads GROUP BY item_id")
        return {r["item_id"]: int(r["n"]) for r in rows}

    def top(self, family: str | None, origin: str | None, period: str, limit: int) -> list[tuple[str, int]]:
        days = PERIODS.get(period, 7)
        clauses = ["d.ts >= ?"]
        params: list = [time.time() - days * 86400]
        if family:
            clauses.append("d.family = ?")
            params.append(family)
        if origin:
            clauses.append("i.origin = ?")
            params.append(origin)
        rows = self.db.all(
            "SELECT d.item_id, COUNT(*) AS n FROM downloads d JOIN items i ON i.id = d.item_id "
            f"WHERE {' AND '.join(clauses)} AND i.hidden = 0 GROUP BY d.item_id ORDER BY n DESC, d.item_id LIMIT ?",
            (*params, limit),
        )
        return [(r["item_id"], int(r["n"])) for r in rows]
