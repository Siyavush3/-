"""Настройки системы (ТЗ, администратор: параметры производительности, безопасности и политик доступа,
резервного копирования, журналирования, телефонии и голоса).

Хранятся в таблице settings (ключ → JSON). Каждый параметр описан схемой: значение по умолчанию, тип,
границы — неверное значение не сохраняется. Изменения применяются к работающему серверу сразу
(подписчики on_change), без перезапуска.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Tuple

from store.db import Database

# ключ: (раздел, название, тип, по умолчанию, ограничение, пояснение)
SCHEMA: Dict[str, tuple] = {
    "security.password_min": ("Безопасность", "Минимальная длина пароля", "int", 8, (8, 64),
                              "в пароле обязательны буквы и цифры"),
    "security.max_failed": ("Безопасность", "Неудачных попыток входа до блокировки", "int", 5, (3, 20), ""),
    "security.lock_minutes": ("Безопасность", "Блокировка входа после неудачных попыток, мин", "int", 5, (1, 120), ""),
    "security.session_hours": ("Безопасность", "Срок сеанса, ч", "int", 12, (1, 72),
                               "после — вход заново; действует для новых сеансов"),
    "backup.enabled": ("Резервное копирование", "Копирование по расписанию", "bool", True, None,
                       "ТЗ: не реже одного раза в сутки"),
    "backup.time": ("Резервное копирование", "Время ежедневной копии (ЧЧ:ММ)", "time", "03:00", None, ""),
    "backup.keep": ("Резервное копирование", "Хранить копий", "int", 14, (1, 365), "старые удаляются после новой"),
    "backup.include_materials": ("Резервное копирование", "Копировать учебные материалы", "bool", True, None, ""),
    "logging.level": ("Журналирование", "Уровень журнала", "choice", "INFO", ("DEBUG", "INFO", "WARNING", "ERROR"), ""),
    "logging.requests": ("Журналирование", "Записывать запросы к серверу", "bool", True, None,
                         "метод, адрес, код ответа, время; без содержимого"),
    "logging.max_mb": ("Журналирование", "Объём журнала на диске, МБ", "int", 50, (5, 2000),
                       "файлы журнала по кругу: при превышении удаляется самый старый"),
    "performance.max_lessons": ("Производительность", "Одновременных занятий, не больше", "int", 50, (1, 500),
                                "ТЗ: не менее 20 одновременных сессий"),
    "performance.monitor_s": ("Производительность", "Обновление монитора преподавателя, с", "float", 1.0, (0.5, 10.0), ""),
    "voice.enabled": ("Телефония и голос", "Озвучка реплик заявителя и бригады", "bool", True, None,
                      "выключено — реплики только текстом"),
    "voice.profile": ("Телефония и голос", "Ресурсы озвучки", "choice", "background", ("background", "install"),
                      "background — 1 ядро и низкий приоритет; install — все ядра, когда занятий нет"),
    "voice.asr": ("Телефония и голос", "Распознавание речи оператора (микрофон)", "bool", True, None,
                  "вопросы заявителю голосом; выключено — только кнопками и текстом"),
    "service.maintenance": ("Режим работы", "Режим обслуживания", "bool", False, None,
                            "новые занятия и вход обучающихся закрыты; идущие занятия продолжаются"),
}


class SettingsError(ValueError):
    pass


def _coerce(key: str, value):
    section, title, kind, default, limits, _ = SCHEMA[key]
    if kind == "bool":
        if not isinstance(value, bool):
            raise SettingsError("%s: нужно да или нет" % title)
        return value
    if kind in ("int", "float"):
        try:
            v = int(value) if kind == "int" else float(value)
        except (TypeError, ValueError):
            raise SettingsError("%s: нужно число" % title)
        if isinstance(value, bool) or (kind == "int" and float(value) != v):
            raise SettingsError("%s: нужно целое число" % title)
        lo, hi = limits
        if not lo <= v <= hi:
            raise SettingsError("%s: от %s до %s" % (title, lo, hi))
        return v
    if kind == "time":
        if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise SettingsError("%s: время в виде ЧЧ:ММ" % title)
        return value
    if kind == "choice":
        if value not in limits:
            raise SettingsError("%s: одно из %s" % (title, ", ".join(limits)))
        return value
    raise SettingsError("неизвестный тип")


class Settings:
    def __init__(self, db: Database):
        self.db = db
        self.values: Dict[str, object] = {k: v[3] for k, v in SCHEMA.items()}
        for row in db.query("SELECT key, value FROM settings"):
            if row["key"] in SCHEMA:
                try:
                    self.values[row["key"]] = _coerce(row["key"], json.loads(row["value"]))
                except (ValueError, SettingsError):
                    pass  # испорченное значение — остаётся по умолчанию
        self._listeners: List[Callable[["Settings", Dict[str, object]], None]] = []

    def get(self, key: str):
        return self.values[key]

    def on_change(self, fn: Callable[["Settings", Dict[str, object]], None]) -> None:
        self._listeners.append(fn)
        fn(self, dict(self.values))  # сразу применить текущие значения

    def update(self, changes: Dict[str, object]) -> List[Tuple[str, object, object]]:
        """Проверить все изменения, сохранить и применить. Возвращает [(ключ, было, стало)]."""
        unknown = [k for k in changes if k not in SCHEMA]
        if unknown:
            raise SettingsError("неизвестные параметры: %s" % ", ".join(unknown))
        new = {k: _coerce(k, v) for k, v in changes.items()}
        diff = [(k, self.values[k], v) for k, v in new.items() if self.values[k] != v]
        if not diff:
            return []
        with self.db.transaction():
            for k, _, v in diff:
                self.db.execute("DELETE FROM settings WHERE key = ?", (k,))
                self.db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))
        for k, _, v in diff:
            self.values[k] = v
        changed = {k: v for k, _, v in diff}
        for fn in self._listeners:
            fn(self, changed)
        return diff

    def describe(self) -> List[dict]:
        return [{"key": k, "section": s, "title": t, "type": kind, "default": d,
                 "limits": list(lim) if lim else None, "hint": hint, "value": self.values[k]}
                for k, (s, t, kind, d, lim, hint) in SCHEMA.items()]
