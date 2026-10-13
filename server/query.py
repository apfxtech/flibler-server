"""The one list query behind v0 and v1: filters are optional conditions, the
visibility rule is section 8.1, and sorting covers both contracts."""

import time

from .stats import PERIODS

SORT_FIELDS = {"updated_at", "created_at", "name", "downloads", "downloads_week", "downloads_month", "downloads_season", "downloads_year"}


class ListQuery:
    def __init__(self, state):
        self.state = state
        self.db = state.db

    def visibility_sql(self, target: str | None, api: str | None) -> tuple[str, list]:
        clause = (
            "(i.family = 'animation' OR EXISTS (SELECT 1 FROM builds b WHERE b.version_id = i.current_version_id "
            "AND b.result = 'ok'"
        )
        params: list = []
        if target:
            clause += " AND b.target = ?"
            params.append(target)
        if api:
            clause += " AND b.api = ?"
            params.append(api)
        clause += "))"
        return clause, params

    def _where(
        self,
        *,
        family: str,
        target: str | None,
        api: str | None,
        origins: list[str] | None,
        category_id: str | None,
        query: str | None,
        ids: list[str] | None,
        author_id: str | None,
        unfiltered: bool,
        since: float | None,
    ) -> tuple[list[str], list]:
        clauses = ["i.family = ?", "i.hidden = 0", "i.current_version_id IS NOT NULL"]
        params: list = [family]
        if not unfiltered:
            vis, vis_params = self.visibility_sql(target, api)
            clauses.append(vis)
            params.extend(vis_params)
        if origins:
            clauses.append(f"i.origin IN ({','.join('?' * len(origins))})")
            params.extend(origins)
        if category_id:
            subtree = self.state.catalog.category_subtree(family, category_id)
            clauses.append(f"i.category_id IN ({','.join('?' * len(subtree))})")
            params.extend(subtree)
        if query:
            like = f"%{query.lower()}%"
            clauses.append("(lower(i.name) LIKE ? OR lower(i.alias) LIKE ? OR lower(i.short_description) LIKE ? OR lower(i.author) LIKE ?)")
            params.extend([like, like, like, like])
        if ids:
            clauses.append(
                f"(i.id IN ({','.join('?' * len(ids))}) OR i.id IN (SELECT item_id FROM item_ids WHERE alt_id IN ({','.join('?' * len(ids))})))"
            )
            params.extend(ids)
            params.extend(ids)
        if author_id:
            clauses.append("EXISTS (SELECT 1 FROM json_each(i.authors) a WHERE json_extract(a.value, '$.id') = ?)")
            params.append(author_id.lower())
        if since is not None:
            clauses.append("i.updated_at >= ?")
            params.append(since)
        return clauses, params

    def run(
        self,
        *,
        family: str,
        target: str | None = None,
        api: str | None = None,
        origins: list[str] | None = None,
        category_id: str | None = None,
        query: str | None = None,
        ids: list[str] | None = None,
        author_id: str | None = None,
        sort_by: str = "updated_at",
        sort_order: int = -1,
        limit: int = 48,
        offset: int = 0,
        unfiltered: bool = False,
        since: float | None = None,
    ) -> list:
        clauses, params = self._where(
            family=family, target=target, api=api, origins=origins, category_id=category_id, query=query,
            ids=ids, author_id=author_id, unfiltered=unfiltered, since=since,
        )
        join = ""
        if sort_by not in SORT_FIELDS:
            sort_by = "updated_at"
        if sort_by == "name":
            order = "lower(i.name)"
        elif sort_by.startswith("downloads"):
            period = sort_by.removeprefix("downloads").lstrip("_")
            if period in PERIODS:
                join = "LEFT JOIN (SELECT item_id, COUNT(*) AS n FROM downloads WHERE ts >= ? GROUP BY item_id) d ON d.item_id = i.id"
                params.insert(0, time.time() - PERIODS[period] * 86400)
            else:
                join = "LEFT JOIN (SELECT item_id, COUNT(*) AS n FROM downloads GROUP BY item_id) d ON d.item_id = i.id"
            order = "COALESCE(d.n, 0)"
        else:
            order = f"i.{sort_by}"
        direction = "ASC" if sort_order > 0 else "DESC"
        sql = (
            f"SELECT i.* FROM items i {join} WHERE {' AND '.join(clauses)} "
            f"ORDER BY {order} {direction}, i.alias ASC LIMIT ? OFFSET ?"
        )
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        return self.db.all(sql, tuple(params))

    def count(self, **filters) -> int:
        """How many rows run() would return without limit and offset."""
        for key in ("sort_by", "sort_order", "limit", "offset"):
            filters.pop(key, None)
        filters.setdefault("unfiltered", False)
        filters.setdefault("since", None)
        for key in ("target", "api", "origins", "category_id", "query", "ids", "author_id"):
            filters.setdefault(key, None)
        clauses, params = self._where(**filters)
        return int(self.db.scalar(f"SELECT COUNT(*) FROM items i WHERE {' AND '.join(clauses)}", tuple(params)))

    def count_by_category(self, family: str, target: str | None, api: str | None) -> dict[str, int]:
        """Visible items per category; a subcategory also counts toward its root."""
        vis, params = self.visibility_sql(target, api)
        rows = self.db.all(
            f"SELECT i.category_id, COUNT(*) AS n FROM items i WHERE i.family = ? AND i.hidden = 0 AND {vis} GROUP BY i.category_id",
            (family, *params),
        )
        out: dict[str, int] = {}
        for row in rows:
            top = self.state.catalog.top_category(family, row["category_id"])
            out[top] = out.get(top, 0) + int(row["n"])
            if row["category_id"] != top:
                out[row["category_id"]] = out.get(row["category_id"], 0) + int(row["n"])
        return out
