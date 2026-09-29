"""База данных: SQLite (разработка, одна машина) или PostgreSQL ≥ 12 (учебный класс) — один и тот же SQL.

Адрес — переменная A112_DB:
    sqlite:///var/arm112.sqlite                      (по умолчанию; путь от корня проекта)
    postgresql://user:password@host:5432/arm112      (нужен пакет psycopg)

Запросы пишутся с «?», для PostgreSQL знаки заменяются на %s — поэтому в тексте запросов
не должно быть символа %. JSON хранится текстом, время — секундами эпохи, логические поля — 0/1:
так схема одинакова в обеих СУБД.

Вызовы синхронные и короткие (единицы миллисекунд): сервер делает их прямо из цикла событий,
блокировка сериализует доступ к соединению, в том числе на время транзакции.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, List, Optional

from arm112.paths import REPO_ROOT

DEFAULT_URL = "sqlite:///var/arm112.sqlite"


def database_url() -> str:
    return os.environ.get("A112_DB") or DEFAULT_URL


class Database:
    def __init__(self, url: Optional[str] = None):
        self.url = url or database_url()
        self.lock = threading.RLock()
        self._depth = 0
        if self.url.startswith("sqlite:"):
            self.dialect = "sqlite"
            path = self.url.split(":///", 1)[1] if ":///" in self.url else self.url.split(":", 1)[1]
            if path != ":memory:":
                p = Path(path)
                if not p.is_absolute():
                    p = REPO_ROOT / p
                p.parent.mkdir(parents=True, exist_ok=True)
                path = str(p)
            self.path = path
            self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
            if path != ":memory:":
                self.conn.execute("PRAGMA journal_mode = WAL")      # читатели не ждут писателя
                self.conn.execute("PRAGMA synchronous = NORMAL")    # тысячи записей в секунду при WAL
        elif self.url.startswith(("postgresql://", "postgres://")):
            import psycopg
            from psycopg.rows import dict_row
            self.dialect = "postgresql"
            self.path = None
            self.conn = psycopg.connect(self.url, autocommit=True, row_factory=dict_row)
        else:
            raise ValueError("A112_DB: ожидается sqlite:///… или postgresql://…, получено %r" % self.url)

    # ---------- запросы ----------
    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgresql" else sql

    def execute(self, sql: str, params: Iterable[Any] = ()):
        with self.lock:
            return self.conn.execute(self._sql(sql), tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(self._sql(sql), tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Iterable[Any] = ()):
        row = self.one(sql, params)
        return next(iter(row.values())) if row else None

    def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        """INSERT с автоматическим id; возвращает id новой строки."""
        with self.lock:
            if self.dialect == "postgresql":
                return self.conn.execute(self._sql(sql) + " RETURNING id", tuple(params)).fetchone()["id"]
            return self.conn.execute(sql, tuple(params)).lastrowid

    @contextmanager
    def transaction(self):
        """Вложенные вызовы допустимы: фиксирует только внешняя транзакция."""
        with self.lock:
            outer = self._depth == 0
            if outer:
                self.conn.execute("BEGIN")
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self.conn.execute("COMMIT")

    def close(self) -> None:
        with self.lock:
            self.conn.close()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(text: Optional[str], default: Any = None) -> Any:
    return json.loads(text) if text else default
