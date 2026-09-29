"""Журнал аудита (ТЗ: аудит всех действий пользователей, контроль целостности, хранение ≥ 6 месяцев).

Каждая запись содержит хеш предыдущей: изменить или удалить запись в середине журнала незаметно
нельзя — проверка целостности укажет первую испорченную запись. Записи не удаляются вообще:
срок хранения «не менее 6 месяцев» выполняется с запасом.
"""
from __future__ import annotations

import hashlib
import time
from typing import Callable, List, Optional

from store.db import Database, dumps, loads

GENESIS = "0" * 64


def _digest(prev: str, at: float, user_id, login, action, obj, details: str, ip) -> str:
    payload = "|".join([prev, "%.6f" % at, str(user_id or ""), login or "", action, obj or "", details, ip or ""])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Audit:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db = db
        self.clock = clock

    def log(self, action: str, user=None, obj: Optional[str] = None, details: Optional[dict] = None,
            ip: Optional[str] = None) -> None:
        """user — app.auth.User или None (например, неудачный вход неизвестного логина)."""
        user_id = getattr(user, "id", None)
        login = getattr(user, "login", None) or (details or {}).get("login")
        text = dumps(details or {})
        with self.db.transaction():
            prev = self.db.scalar("SELECT hash FROM audit ORDER BY id DESC LIMIT 1") or GENESIS
            at = round(self.clock(), 6)
            self.db.insert("INSERT INTO audit (at, user_id, login, action, object, details, ip, prev_hash, hash)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (at, user_id, login, action, obj, text, ip, prev,
                            _digest(prev, at, user_id, login, action, obj, text, ip)))

    def list(self, limit: int = 200, action: Optional[str] = None, login: Optional[str] = None,
             before_id: Optional[int] = None) -> List[dict]:
        where, params = [], []
        if action:
            where.append("action LIKE ?")
            params.append(action.replace("*", "") + "%")
        if login:
            where.append("login = ?")
            params.append(login)
        if before_id:
            where.append("id < ?")
            params.append(before_id)
        sql = "SELECT id, at, user_id, login, action, object, details, ip FROM audit"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self.db.query(sql + " ORDER BY id DESC LIMIT ?", (*params, min(max(limit, 1), 1000)))
        for r in rows:
            r["details"] = loads(r["details"], {})
        return rows

    def verify(self) -> dict:
        """Проверка цепочки хешей от первой записи до последней."""
        prev, checked = GENESIS, 0
        for r in self.db.query("SELECT * FROM audit ORDER BY id"):
            expected = _digest(prev, r["at"], r["user_id"], r["login"], r["action"], r["object"], r["details"], r["ip"])
            if r["prev_hash"] != prev or r["hash"] != expected:
                return {"ok": False, "checked": checked, "broken_at": r["id"]}
            prev, checked = r["hash"], checked + 1
        return {"ok": True, "checked": checked, "broken_at": None}
