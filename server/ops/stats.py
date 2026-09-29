"""Статистика использования (ТЗ, администратор: «анализировать статистику использования системы»).

По дням — входы, неудачные попытки, активные пользователи, проведённые и прерванные занятия, результаты
и средний балл; итоги — учётные записи по ролям, занятия, сценарии, материалы. Персональные данные
не выводятся: только количества.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Dict


def _day(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def usage(db, days: int = 14, now: float = None) -> dict:
    now = time.time() if now is None else now
    lt = time.localtime(now - (days - 1) * 86400)
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    per: Dict[str, dict] = {}
    for i in range(days):
        per[_day(start + i * 86400 + 3600)] = {"logins": 0, "failed": 0, "active_users": 0, "lessons": 0,
                                                "interrupted": 0, "results": 0, "avg_score": None}
    users = defaultdict(set)
    for r in db.query("SELECT at, action, user_id FROM audit WHERE at >= ? AND action IN ('login.ok', 'login.fail')", (start,)):
        d = per.get(_day(r["at"]))
        if d is None:
            continue
        if r["action"] == "login.ok":
            d["logins"] += 1
            users[_day(r["at"])].add(r["user_id"])
        else:
            d["failed"] += 1
    for day, ids in users.items():
        per[day]["active_users"] = len(ids)
    for r in db.query("SELECT finished_at, state FROM lessons WHERE finished_at >= ?", (start,)):
        d = per.get(_day(r["finished_at"]))
        if d is not None:
            d["interrupted" if r["state"] == "прервано" else "lessons"] += 1
    scores = defaultdict(list)
    for r in db.query("SELECT created_at, score, expert_score FROM results WHERE created_at >= ?", (start,)):
        scores[_day(r["created_at"])].append(r["expert_score"] if r["expert_score"] is not None else r["score"])
    for day, xs in scores.items():
        if day in per:
            per[day]["results"] = len(xs)
            per[day]["avg_score"] = round(sum(xs) / len(xs))
    roles = {r["role"]: r["n"] for r in db.query("SELECT role, COUNT(*) AS n FROM users GROUP BY role")}
    totals = {
        "users": roles, "blocked": db.scalar("SELECT COUNT(*) AS n FROM users WHERE blocked = 1") or 0,
        "lessons": db.scalar("SELECT COUNT(*) AS n FROM lessons") or 0,
        "interrupted": db.scalar("SELECT COUNT(*) AS n FROM lessons WHERE state = 'прервано'") or 0,
        "results": db.scalar("SELECT COUNT(*) AS n FROM results") or 0,
        "scenarios": {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) AS n FROM scenarios GROUP BY status")},
        "groups": db.scalar("SELECT COUNT(*) AS n FROM study_groups WHERE archived = 0") or 0,
        "tasks": db.scalar("SELECT COUNT(*) AS n FROM tasks WHERE archived = 0") or 0,
        "materials": db.scalar("SELECT COUNT(*) AS n FROM materials") or 0,
        "materials_mb": round((db.scalar("SELECT SUM(size) AS s FROM materials") or 0) / 2 ** 20, 1),
        "audit_records": db.scalar("SELECT COUNT(*) AS n FROM audit") or 0,
    }
    return {"days": [dict(v, date=k) for k, v in per.items()], "totals": totals}
