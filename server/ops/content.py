"""Пакетное обновление учебного контента (ТЗ: «механизм импорта обновлений: пакетное обновление учебных
материалов и сценариев (в ручном режиме)»; администратор: «выполнять обновление ПО (пакетное обновление)»).

Пакет — zip с файлами scenarios/*.json (формат data/scenarios). Каждый сценарий проходит ту же проверку,
что пакеты при старте сервера; негодные не ставятся, причина — в отчёте. Перед установкой сервер делает
резервную копию (ограничение ТЗ: без процедуры резервного копирования критичные данные не меняются).
Обновление самой программы — дистрибутивом (docs), не через веб-интерфейс.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Callable, List

MAX_FILE = 20 * 2 ** 20
MAX_TOTAL = 200 * 2 ** 20


class UpdateError(ValueError):
    pass


def inspect(data: bytes, build_pack: Callable[[dict], object]) -> dict:
    """Разобрать пакет, не устанавливая: что годится и что нет."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UpdateError("это не zip-архив")
    ok: List[dict] = []
    rejected: List[dict] = []
    total = 0
    for info in z.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        parts = name.split("/")
        if len(parts) != 2 or parts[0] != "scenarios" or not parts[1].endswith(".json") or ".." in name:
            rejected.append({"file": name, "error": "в пакете допускаются только файлы scenarios/*.json"})
            continue
        total += info.file_size
        if info.file_size > MAX_FILE or total > MAX_TOTAL:
            rejected.append({"file": name, "error": "слишком большой файл"})
            continue
        try:
            raw = json.loads(z.read(info).decode("utf-8"))
            if not isinstance(raw, dict) or not (raw.get("cards") or raw.get("calls")):
                raise ValueError("нет карточек (cards) или вызовов (calls)")
            pack = build_pack(raw)
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            rejected.append({"file": name, "error": str(exc)[:300]})
            continue
        ok.append({"file": parts[1], "raw": raw, "pack": pack, "id": pack.id, "title": pack.title})
    return {"ok": ok, "rejected": rejected}


def install(checked: dict, scenarios_dir, packs: dict) -> dict:
    """Записать годные сценарии в data/scenarios и сразу подключить к серверу."""
    scenarios_dir = Path(scenarios_dir)
    installed, replaced = [], []
    for item in checked["ok"]:
        target = scenarios_dir / item["file"]
        (replaced if target.exists() or item["id"] in packs else installed).append(item["title"])
        target.write_text(json.dumps(item["raw"], ensure_ascii=False, indent=1), encoding="utf-8")
        packs[item["id"]] = item["pack"]
    return {"installed": installed, "replaced": replaced,
            "rejected": [{"file": r["file"], "error": r["error"]} for r in checked["rejected"]]}
