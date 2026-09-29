"""Демонстрационная история занятий для показа аналитики (узел 6).

Аналитика, прогноз и тепловая карта оживают только на истории из нескольких занятий. Для демонстрации
команда `python server/manage.py seed-history` создаёт демо-группу из демо-учёток и прошлые занятия
с результатами. Данные учебные и помечены: название занятия начинается с «Демо:», в параметрах — demo.
Баллы — по той же модели кривых обучения, на которой проверен прогноз (приближение к потолку с шумом),
ошибки — по категориям рубрик с разной «слабостью» у каждого обучающегося.
"""
from __future__ import annotations

import math
import random
import time
from typing import Dict, List

from store.db import Database
from store.repos import Groups, Lessons, Results

DDS_DETAILS = {
    1: ["первичный статус через %d с при нормативе 30 с", "статус реагирования не проставлен"],
    2: ["следовало проставить «Не принята» с комментарием", "итог работ — «Работы завершены», проставлен «Отказ от выполнения работ»"],
    3: ["происшествие профильное — следовало «Принята»"],
    4: ["«Не принята» без комментария"],
    5: ["в комментарии не указано: передан", "в комментарии не указано: оэк"],
    6: ["после доклада «Бригада на месте» не проставлен статус «Прибытие»", "статус «Проведение работ» без комментария"],
    8: ["«информацыю» → информацию", "«сантехнек» → сантехник", "два пробела подряд: «передано  в УК»"],
}
C112_DETAILS = {
    1: ["выбрано «Нет света в квартире», а по вызову — «Течь (прорыв трубы)»"],
    3: ["не отмечено: угроза людям"],
    4: ["дом: указано «15», верно — «17»", "улица: указано «Рогова», верно — «Расплетина»"],
    5: ["не указано или неверно: квартира, подъезд"],
    6: ["не заполнено: статус заявителя"],
    7: ["не упомянуто: дым / задымл"],
    8: ["«возгарание» → возгорание", "«пастрадавших» → пострадавших"],
    9: ["карточка заполнена за %d с при нормативе 80 с"],
    11: ["вызов принят через %d с при нормативе 15 с"],
}
PLAN = [("действия с карточками", "Демо: ДДС Щукино, базовая"), ("карточки", "Демо: вызовы 112, базовая"),
        ("действия с карточками", "Демо: ДДС Строгино, средняя"), ("карточки", "Демо: вызовы 112, средняя"),
        ("действия с карточками", "Демо: ДДС Отрадное, средняя"), ("карточки", "Демо: вызовы 112, высокая"),
        ("действия с карточками", "Демо: ДДС Марьино, высокая")]


def seed_history(db: Database, teacher_id: int, students: List[dict], rubric_dds: Dict[int, dict],
                 rubric_112: Dict[str, dict], seed: int = 112, days: int = 21) -> int:
    """Создаёт демо-группу и прошлые занятия. Возвращает число занятий. Повторный запуск ничего не дублирует."""
    if db.scalar("SELECT COUNT(*) AS n FROM lessons WHERE title LIKE 'Демо:%%' AND teacher_id = ?", (teacher_id,)):
        return 0
    rng = random.Random(seed)
    now = time.time()
    groups = Groups(db)
    gid = groups.create(teacher_id, "Демо-группа (история для аналитики)")
    groups.set_members(gid, [s["id"] for s in students])
    profiles = []
    for s in students:  # у каждого — свой старт, скорость и «слабые места»
        profiles.append({"s0": rng.uniform(35, 70), "k": rng.uniform(0.05, 0.45), "cap": rng.uniform(82, 98),
                         "sd": rng.uniform(3, 7), "weak_dds": rng.sample([1, 2, 5, 6, 8], 2),
                         "weak_112": rng.sample([3, 4, 5, 7, 8, 9], 2)})
    for i, (mode, title) in enumerate(PLAN):
        t_end = now - (len(PLAN) - i) * days / len(PLAN) * 86400
        lessons, results = Lessons(db, lambda: t_end), Results(db, lambda: t_end)
        lid = lessons.create(teacher_id, mode, "demo", title, {"demo": True, "time_scale": 1.0}, "завершено", group_id=gid)
        lessons.set_state(lid, "завершено", started_at=t_end - 45 * 60, finished_at=t_end)
        dds = mode != "карточки"
        cats = [{"category": c, "title": v["title"], "weight": v["weight"]} for c, v in sorted(rubric_dds.items())] if dds else \
            [{"category": int(c), "title": v["title"], "weight": v["weight"]} for c, v in sorted(rubric_112.items(), key=lambda x: int(x[0]))]
        weights = {c["category"]: c["weight"] for c in cats}
        participants = []
        for s, p in zip(students, profiles):
            target = p["cap"] - (p["cap"] - p["s0"]) * math.exp(-p["k"] * i) + rng.gauss(0, p["sd"])
            findings, penalty = [], 0.0
            cards = 10 if dds else 8
            weak = p["weak_dds"] if dds else p["weak_112"]
            details = DDS_DETAILS if dds else C112_DETAILS
            while penalty < 100 - target - 3 and len(findings) < 25:  # ошибки набираются до нужного балла
                cat = rng.choice(weak) if rng.random() < 0.7 else rng.choice(list(details))
                text = rng.choice(details[cat])
                text = text % rng.randint(35, 120) if "%d" in text else text
                findings.append({"category": cat, "title": next(c["title"] for c in cats if c["category"] == cat),
                                 "card": str(36900000 + lid * 1000 + 101 + rng.randrange(cards)), "status": "", "detail": text})
                penalty += weights[cat] / (1 if dds else cards)
            score = max(0, min(100, round(100 - penalty)))
            summary = {"participant": "u%d" % s["id"], "role": "ДДС" if dds else "Оператор 112", "score": score,
                       "findings": findings, "name": s["name"], "cards": cards, "categories": cats,
                       "criteria": {"pass_score": 70, "max_errors": None, "text_check": True}}
            if not dds:
                fills = [max(25.0, rng.gauss(95 - i * 4, 15)) for _ in range(cards)]
                summary["timing"] = {"avg_fill_s": round(sum(fills) / cards, 1), "max_fill_s": round(max(fills), 1),
                                     "norm_s": 80.0, "over_norm": sum(1 for x in fills if x > 80)}
            participants.append(summary)
        results.save(lid, participants)
    return len(PLAN)
