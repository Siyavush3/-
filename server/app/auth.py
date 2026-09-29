"""Учётные записи и сеансы (ТЗ: многоуровневая аутентификация, RBAC, блокировка учётных записей).

Уровни защиты входа:
  1. пароль (PBKDF2-SHA256, политика сложности, временный пароль с обязательной сменой);
  2. защита от перебора: после 5 неудачных попыток вход по логину закрыт на 5 минут;
  3. сеанс с ограниченным сроком (12 ч) и отзывом при выходе, блокировке и смене пароля;
  4. роли (администратор, преподаватель, обучающийся) — права проверяются на каждом запросе;
  5. для опасных действий администратора — повторный ввод пароля (см. app.main).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from app.security import (MIN_PASSWORD, hash_password, new_token, policy_error, temporary_password, token_hash,
                          verify_password)
from domain.lesson import Actor, ActorKind
from store.db import Database

ROLES = {"admin": ActorKind.ADMIN, "teacher": ActorKind.TEACHER, "student": ActorKind.STUDENT}
ROLE_TITLES = {"admin": "администратор", "teacher": "преподаватель", "student": "обучающийся"}
SESSION_TTL_S = 12 * 3600
MAX_FAILED = 5
LOCK_S = 300.0
LAST_SEEN_STEP_S = 60.0  # отметку активности пишем не чаще раза в минуту


class AuthError(Exception):
    """Сообщение безопасно показывать пользователю: не раскрывает, существует ли логин."""


@dataclass(frozen=True)
class User:
    id: int
    login: str
    name: str
    role: str
    must_change: bool = False
    blocked: bool = False

    @property
    def pid(self) -> str:
        """Идентификатор участника в движке занятий."""
        return "u%d" % self.id

    @property
    def actor(self) -> Actor:
        return Actor(self.pid, ROLES[self.role])

    @property
    def is_teacher(self) -> bool:
        return self.role == "teacher"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_dict(self) -> dict:
        return {"id": self.id, "login": self.login, "name": self.name, "role": self.role,
                "must_change": self.must_change}


def _user(row: dict) -> User:
    return User(row["id"], row["login"], row["name"], row["role"], bool(row["must_change"]), bool(row["blocked"]))


def normalize_login(login: str) -> str:
    return login.strip().lower()


class Accounts:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db = db
        self.clock = clock
        # политика безопасности: значения по умолчанию, администратор меняет их в настройках (узел 7)
        self.password_min = MIN_PASSWORD
        self.max_failed = MAX_FAILED
        self.lock_s = LOCK_S
        self.session_ttl_s = SESSION_TTL_S

    # ---------- учётные записи ----------
    def create(self, login: str, name: str, role: str, password: Optional[str] = None,
               must_change: bool = True) -> Tuple[User, str]:
        """Новая учётная запись. Без пароля — временный, который возвращается один раз."""
        login = normalize_login(login)
        name = " ".join(name.split())
        if role not in ROLES:
            raise ValueError("роль — admin, teacher или student")
        if not login or not all(c.isalnum() or c in "._-" for c in login) or len(login) > 40:
            raise ValueError("логин: латиница, цифры, точка, дефис, подчёркивание; до 40 символов")
        if not name:
            raise ValueError("нужно имя")
        if self.db.one("SELECT id FROM users WHERE login = ?", (login,)):
            raise ValueError("логин «%s» уже занят" % login)
        if password is None:
            password = temporary_password(self.password_min)
        else:
            err = policy_error(password, self.password_min)
            if err:
                raise ValueError(err)
        uid = self.db.insert(
            "INSERT INTO users (login, name, role, password_hash, must_change, blocked, failed_logins, created_at)"
            " VALUES (?, ?, ?, ?, ?, 0, 0, ?)", (login, name, role, hash_password(password), int(must_change), self.clock()))
        return self.get(uid), password

    def get(self, user_id: int) -> Optional[User]:
        row = self.db.one("SELECT * FROM users WHERE id = ?", (user_id,))
        return _user(row) if row else None

    def by_login(self, login: str) -> Optional[User]:
        row = self.db.one("SELECT * FROM users WHERE login = ?", (normalize_login(login),))
        return _user(row) if row else None

    def list(self, role: Optional[str] = None) -> List[dict]:
        sql = "SELECT id, login, name, role, blocked, must_change, created_at, last_login, locked_until FROM users"
        rows = self.db.query(sql + (" WHERE role = ?" if role else "") + " ORDER BY role, login", (role,) if role else ())
        for r in rows:
            r["blocked"] = bool(r["blocked"])
            r["must_change"] = bool(r["must_change"])
        return rows

    def count(self) -> int:
        return self.db.scalar("SELECT COUNT(*) AS n FROM users") or 0

    def update(self, user_id: int, name: Optional[str] = None, role: Optional[str] = None) -> User:
        if role is not None and role not in ROLES:
            raise ValueError("роль — admin, teacher или student")
        if name is not None:
            name = " ".join(name.split())
            if not name:
                raise ValueError("нужно имя")
            self.db.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
        if role is not None:
            self.db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
            self.revoke_sessions(user_id)  # новые права — только с новым входом
        return self.get(user_id)

    def set_blocked(self, user_id: int, blocked: bool) -> None:
        self.db.execute("UPDATE users SET blocked = ?, failed_logins = 0, locked_until = NULL WHERE id = ?",
                        (int(blocked), user_id))
        if blocked:
            self.revoke_sessions(user_id)

    def reset_password(self, user_id: int) -> str:
        password = temporary_password(self.password_min)
        self.db.execute("UPDATE users SET password_hash = ?, must_change = 1, failed_logins = 0, locked_until = NULL"
                        " WHERE id = ?", (hash_password(password), user_id))
        self.revoke_sessions(user_id)
        return password

    def change_password(self, user_id: int, old: str, new: str, keep_token: Optional[str] = None) -> None:
        row = self.db.one("SELECT password_hash FROM users WHERE id = ?", (user_id,))
        if not row or not verify_password(old, row["password_hash"]):
            raise AuthError("текущий пароль указан неверно")
        err = policy_error(new, self.password_min)
        if err:
            raise ValueError(err)
        if new == old:
            raise ValueError("новый пароль должен отличаться от текущего")
        self.db.execute("UPDATE users SET password_hash = ?, must_change = 0 WHERE id = ?", (hash_password(new), user_id))
        self.revoke_sessions(user_id, except_hash=token_hash(keep_token) if keep_token else None)

    def check_password(self, user_id: int, password: str) -> bool:
        row = self.db.one("SELECT password_hash FROM users WHERE id = ?", (user_id,))
        return bool(row) and verify_password(password, row["password_hash"])

    # ---------- вход и сеансы ----------
    def authenticate(self, login: str, password: str, ip: Optional[str] = None) -> Tuple[str, User]:
        now = self.clock()
        row = self.db.one("SELECT * FROM users WHERE login = ?", (normalize_login(login),))
        if row is None:
            verify_password(password, hash_password("x"))  # то же время ответа, что и для существующего логина
            raise AuthError("неверный логин или пароль")
        if row["blocked"]:
            raise AuthError("учётная запись заблокирована — обратитесь к администратору")
        if row["locked_until"] and row["locked_until"] > now:
            raise AuthError("слишком много неудачных попыток — вход закрыт на %d мин" % max(1, round((row["locked_until"] - now) / 60)))
        if not verify_password(password, row["password_hash"]):
            failed = row["failed_logins"] + 1
            locked = now + self.lock_s if failed >= self.max_failed else None
            self.db.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                            (0 if locked else failed, locked, row["id"]))
            if locked:
                raise AuthError("слишком много неудачных попыток — вход закрыт на %d мин" % max(1, self.lock_s // 60))
            raise AuthError("неверный логин или пароль")
        token = new_token()
        with self.db.transaction():
            self.db.execute("UPDATE users SET failed_logins = 0, locked_until = NULL, last_login = ? WHERE id = ?",
                            (now, row["id"]))
            self.db.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen, ip)"
                            " VALUES (?, ?, ?, ?, ?, ?)", (token_hash(token), row["id"], now, now + self.session_ttl_s, now, ip))
        return token, _user(row)

    def session_user(self, token: Optional[str]) -> Optional[User]:
        if not token:
            return None
        now = self.clock()
        row = self.db.one("SELECT s.token_hash, s.expires_at, s.last_seen, u.* FROM sessions s"
                          " JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", (token_hash(token),))
        if row is None or row["expires_at"] <= now or row["blocked"]:
            return None
        if now - row["last_seen"] > LAST_SEEN_STEP_S:
            self.db.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, row["token_hash"]))
        return _user(row)

    def logout(self, token: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))

    def revoke_sessions(self, user_id: int, except_hash: Optional[str] = None) -> None:
        if except_hash:
            self.db.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash <> ?", (user_id, except_hash))
        else:
            self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def purge_sessions(self) -> int:
        cur = self.db.execute("DELETE FROM sessions WHERE expires_at <= ?", (self.clock(),))
        return cur.rowcount or 0
