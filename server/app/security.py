"""Пароли и токены: только стандартная библиотека (изолированный контур, без внешних зависимостей)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import string
from typing import Optional

PBKDF2_ITERATIONS = 200_000   # ≈ 0,1 с на i5 — перебор дорогой, вход по-прежнему мгновенный
MIN_PASSWORD = 8


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, _b64(salt), _b64(digest))


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(actual, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def policy_error(password: str, min_length: int = MIN_PASSWORD) -> Optional[str]:
    """Требования к паролю. None — пароль подходит. Минимальную длину задаёт администратор (узел 7)."""
    if len(password) < min_length:
        return "пароль короче %d символов" % min_length
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return "в пароле нужны и буквы, и цифры"
    return None


def temporary_password(length: int = 10) -> str:
    """Временный пароль для новой учётной записи: читаемый, без похожих символов, проходит политику."""
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
    length = max(10, length)
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if policy_error(value, length) is None:
            return value


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """В базе хранится только хеш токена: утечка таблицы сессий не даёт войти."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
