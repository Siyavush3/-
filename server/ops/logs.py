"""Системный журнал (ТЗ: «JSON для логов и журналов», администратор: просмотр системных журналов,
параметры журналирования, отчёты об ошибках и сбоях).

Журнал — строки JSON в var/logs/arm112.log, по кругу (объём задаёт администратор). В журнал пишутся
события сервера и, если включено, запросы: метод, адрес, код ответа, время — без содержимого и паролей.
Сбои (ответ 5xx, исключения) дополнительно копятся в памяти для отчёта об ошибках.
Журнал аудита действий пользователей — отдельно, в базе, с контролем целостности (app.audit).
"""
from __future__ import annotations

import collections
import json
import logging
import logging.handlers
import time
from pathlib import Path
from typing import Deque, List, Optional

LOGGER = "arm112"
FILES = 5  # текущий файл и четыре предыдущих


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"at": round(record.created, 3), "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        entry.update(getattr(record, "fields", {}) or {})
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)[-2000:]
        return json.dumps(entry, ensure_ascii=False)


class SystemLog:
    def __init__(self, directory, max_mb: int = 50, level: str = "INFO"):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "arm112.log"
        self.logger = logging.getLogger(LOGGER)
        self.logger.propagate = False
        self.handler: Optional[logging.Handler] = None
        self.errors: Deque[dict] = collections.deque(maxlen=500)
        self.requests = True
        self.configure(max_mb, level)

    def configure(self, max_mb: Optional[int] = None, level: Optional[str] = None) -> None:
        if max_mb is not None:
            if self.handler is not None:
                self.logger.removeHandler(self.handler)
                self.handler.close()
            self.handler = logging.handlers.RotatingFileHandler(self.path, maxBytes=max(1, max_mb // FILES) * 2 ** 20,
                                                                backupCount=FILES - 1, encoding="utf-8")
            self.handler.setFormatter(JsonFormatter())
            self.logger.addHandler(self.handler)
        if level is not None:
            self.logger.setLevel(getattr(logging, level))

    def close(self) -> None:
        if self.handler is not None:
            self.logger.removeHandler(self.handler)
            self.handler.close()
            self.handler = None

    def log(self, level: int, msg: str, **fields) -> None:
        self.logger.log(level, msg, extra={"fields": fields})

    def request(self, method: str, path: str, status: int, ms: float, user: Optional[int]) -> None:
        if self.requests:
            self.log(logging.INFO if status < 500 else logging.ERROR, "%s %s %d" % (method, path, status),
                     kind="request", method=method, path=path, status=status, ms=round(ms, 1),
                     **({"user": user} if user is not None else {}))

    def failure(self, source: str, message: str, **fields) -> None:
        """Сбой для отчёта администратора: в журнал и в память (последние 500)."""
        entry = dict({"at": time.time(), "source": source, "message": message[:500]}, **fields)
        self.errors.append(entry)
        self.log(logging.ERROR, message, kind="failure", source=source, **fields)

    def read(self, limit: int = 300, level: Optional[str] = None, text: str = "") -> List[dict]:
        """Последние записи (новые сверху) из текущего и предыдущих файлов, с фильтром."""
        order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        floor = order.get(level or "", 0)
        text = text.lower()
        out: List[dict] = []
        if self.handler is not None:
            self.handler.flush()
        for i in range(FILES):
            path = self.path if i == 0 else Path("%s.%d" % (self.path, i))
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if order.get(entry.get("level"), 0) < floor or (text and text not in line.lower()):
                    continue
                out.append(entry)
                if len(out) >= limit:
                    return out
        return out

    def size(self) -> int:
        return sum(p.stat().st_size for p in self.dir.glob("arm112.log*") if p.is_file())
