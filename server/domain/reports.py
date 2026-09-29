"""Отчёты (узел 6).

ТЗ: «Преподаватель формирует отчёт о практическом занятии с информацией о действиях, замечаниях (ошибках),
времени заполнения карточки, отличия времени от нормативного (заданного в системе), а также грамматики»;
«экспорт отчётности в стандартные форматы (Excel, PDF)», «CSV для выгрузки отчётов и статистики».

Отчёт собирается из результатов (оценка, замечания) и журнала событий занятия (что и когда сделал
обучающийся). Печатная форма и PDF — страница web/report.html (печать браузера); CSV и XLSX — здесь.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from typing import Dict, List, Sequence

from domain.analytics import final_score

TEXT = 8  # категория «ошибки в тексте» в обеих рубриках


def _dds_actions(events: Sequence[dict], user_id: int) -> Dict[str, dict]:
    """Карточки ДДС этого обучающегося: когда пришла, какие статусы и доклады, с задержками."""
    cards: Dict[str, dict] = {}
    for e in events:
        d = e["data"]
        if e["kind"] == "card" and ("u%d" % user_id) in (d.get("recipients") or []):
            cards[d["card"]] = {"card": d["card"], "key": d.get("key"), "at": e["at"], "actions": []}
    for e in events:
        d = e["data"]
        card = cards.get(d.get("card"))
        if card is None or e["user_id"] != user_id:
            continue
        if e["kind"] == "status":
            card["actions"].append({"at": e["at"], "after_s": round(e["at"] - card["at"], 1), "what": d.get("status"),
                                    "comment": d.get("comment") or ""})
        elif e["kind"] == "call":
            card["actions"].append({"at": e["at"], "after_s": round(e["at"] - card["at"], 1),
                                    "what": "доклад бригады", "comment": d.get("text") or ""})
    return cards


def lesson_report(lesson: dict, rows: Sequence[dict], events: Sequence[dict], teacher: str = "") -> dict:
    """lesson — строка lessons, rows — результаты (repos.Results._rows), events — журнал занятия."""
    config = lesson.get("config") or {}
    cards_mode = lesson["mode"] == "карточки"
    participants = []
    categories: List[dict] = []
    for r in rows:
        s = r.get("summary") or {}
        categories = s.get("categories") or categories
        findings = r.get("findings") or []
        by_card: Dict[str, List[dict]] = defaultdict(list)
        for f in findings:
            by_card[str(f.get("card") or "")].append(f)
        crit = s.get("criteria") or {}
        score = final_score(r)
        p = {"result_id": r["id"], "name": r["user_name"], "login": r.get("user_login"), "score": score,
             "auto": r["score"], "expert": r.get("expert_score"), "expert_comment": r.get("expert_comment") or "",
             "passed": score >= crit.get("pass_score", 70) and (crit.get("max_errors") is None or len(findings) <= crit["max_errors"]),
             "criteria": crit, "errors": len(findings),
             "by_category": {str(c["category"]): sum(1 for f in findings if int(f["category"]) == int(c["category"]))
                             for c in categories},
             "grammar": [f["detail"] for f in findings if int(f["category"]) == TEXT], "cards": []}
        if cards_mode:
            timing = s.get("timing") or {}
            norm = timing.get("norm_s")
            p["timing"] = timing
            for c in s.get("cards_detail") or []:
                fill = c.get("fill_s")
                p["cards"].append({"card": c["number"], "type": (c.get("gold") or {}).get("type"), "score": c["score"],
                                   "fill_s": fill, "answer_s": c.get("answer_s"), "norm_s": norm,
                                   "over_s": round(fill - norm, 1) if fill is not None and norm else None,
                                   "findings": ["№%d. %s" % (f["category"], f["detail"]) for f in c.get("findings") or []]})
        else:
            norm = float(config.get("primary_norm_s") or 30)
            p["timing"] = {"norm_s": norm}
            for number, card in sorted(_dds_actions(events, r["user_id"]).items(), key=lambda x: x[1]["at"]):
                statuses = [a for a in card["actions"] if a["what"] != "доклад бригады"]
                first = statuses[0]["after_s"] if statuses else None
                p["cards"].append({"card": number, "key": card["key"], "primary_s": first, "norm_s": norm,
                                   "over_s": round(first - norm, 1) if first is not None and first > norm else None,
                                   "actions": card["actions"],
                                   "findings": ["№%d. %s" % (f["category"], f["detail"]) for f in by_card.get(number, [])]})
            delays = [c["primary_s"] for c in p["cards"] if c["primary_s"] is not None]
            p["timing"].update(avg_primary_s=round(sum(delays) / len(delays), 1) if delays else None,
                               over_norm=sum(1 for d in delays if d > norm), cards=len(p["cards"]))
        participants.append(p)
    return {"lesson": {"id": "L%d" % lesson["id"], "title": lesson.get("title"), "mode": lesson["mode"],
                       "teacher": teacher, "state": lesson["state"], "created_at": lesson.get("created_at"),
                       "started_at": lesson.get("started_at"), "finished_at": lesson.get("finished_at"),
                       "config": config},
            "categories": categories, "participants": sorted(participants, key=lambda p: p["name"])}


# ---------- таблицы ----------
def summary_table(report: dict) -> List[List]:
    cats = report["categories"]
    cards_mode = report["lesson"]["mode"] == "карточки"
    head = ["Обучающийся", "Итоговый балл", "Автоматическая оценка", "Экспертная оценка", "Зачёт", "Замечаний",
            "Среднее время заполнения, с" if cards_mode else "Средняя задержка первичного статуса, с",
            "Норматив, с", "Сверх норматива, карточек", "Ошибок в тексте"] + \
           ["№%s %s" % (c["category"], c["title"]) for c in cats] + ["Комментарий преподавателя"]
    out = [head]
    for p in report["participants"]:
        t = p.get("timing") or {}
        out.append([p["name"], p["score"], p["auto"], "" if p["expert"] is None else p["expert"],
                    "да" if p["passed"] else "нет", p["errors"],
                    t.get("avg_fill_s") if cards_mode else t.get("avg_primary_s"), t.get("norm_s"), t.get("over_norm"),
                    len(p["grammar"])] + [p["by_category"].get(str(c["category"]), 0) for c in cats] + [p["expert_comment"]])
    return out


def cards_table(report: dict) -> List[List]:
    cards_mode = report["lesson"]["mode"] == "карточки"
    if cards_mode:
        out = [["Обучающийся", "Карточка", "Тип происшествия (эталон)", "Балл карточки", "Принят через, с",
                "Заполнена за, с", "Норматив, с", "Сверх норматива, с", "Замечания"]]
        for p in report["participants"]:
            for c in p["cards"]:
                out.append([p["name"], c["card"], c.get("type") or "", c["score"], c.get("answer_s"), c.get("fill_s"),
                            c.get("norm_s"), c.get("over_s") or "", "; ".join(c["findings"])])
        return out
    out = [["Обучающийся", "Карточка", "Первичный статус через, с", "Норматив, с", "Сверх норматива, с",
            "Действия (через сколько секунд после поступления)", "Замечания"]]
    for p in report["participants"]:
        for c in p["cards"]:
            acts = "; ".join("%s +%s с%s" % (a["what"], a["after_s"], (": " + a["comment"]) if a["comment"] else "")
                             for a in c["actions"])
            out.append([p["name"], c["card"], c.get("primary_s"), c.get("norm_s"), c.get("over_s") or "", acts,
                        "; ".join(c["findings"])])
    return out


def to_csv(table: Sequence[Sequence]) -> bytes:
    """CSV для Excel с русской локалью: разделитель «;», UTF-8 с меткой порядка байтов."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    for row in table:
        w.writerow(["" if v is None else v for v in row])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def to_xlsx(sheets: Dict[str, Sequence[Sequence]], title: str = "") -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    wb.remove(wb.active)
    for name, table in sheets.items():
        ws = wb.create_sheet(name[:31])
        for row in table:
            ws.append(["" if v is None else v for v in row])
        if table:
            for cell in ws[1]:
                cell.font = Font(bold=True)
                cell.fill = PatternFill("solid", fgColor="E3EEF7")
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            ws.freeze_panes = "A2"
            for i, col in enumerate(zip(*table), start=1):
                width = max(len(str(v)) for v in col if v is not None) if col else 10
                ws.column_dimensions[get_column_letter(i)].width = min(60, max(10, width + 2))
    wb.properties.title = title
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- успеваемость группы (аналитика) ----------
def progress_tables(analysis: dict) -> Dict[str, List[List]]:
    students = analysis["students"]
    lessons: Dict[str, str] = {}
    for s in students:
        for h in s["history"]:
            lessons.setdefault(h["lesson"], h["title"] or h["lesson"])
    order = sorted(lessons, key=lambda l: int(l[1:]))
    perf = [["Обучающийся"] + ["%s %s" % (l, lessons[l]) for l in order] + ["Средний балл", "Прогноз", "Интервал 80%",
                                                                             "Вероятность зачёта"]]
    for s in students:
        by = {h["lesson"]: h["score"] for h in s["history"]}
        f = s["forecast"] or {}
        scores = [h["score"] for h in s["history"]]
        perf.append([s["name"]] + [by.get(l, "") for l in order] +
                    [round(sum(scores) / len(scores)) if scores else "", f.get("next", ""),
                     "%s–%s" % (f["low"], f["high"]) if f else "", f.get("p_pass", "")])
    check = [["Обучающийся", "Занятие", "Прогноз", "Факт", "Ошибка прогноза", "Наивный прогноз (как в прошлый раз)"]]
    for s in students:
        hist = s["history"]
        for i, h in enumerate(hist):
            if h["predicted"] is not None:
                check.append([s["name"], h["lesson"], h["predicted"], h["score"], round(h["score"] - h["predicted"], 1),
                              hist[i - 1]["score"] if i else ""])
    heat = [["Обучающийся", "Режим"] + ["№%s %s" % (c["category"], c["title"]) for m in analysis["categories"]
                                        for c in analysis["categories"][m]]]
    for s in students:
        for mode, h in s["heat"].items():
            row = [s["name"], mode]
            for m in analysis["categories"]:
                for c in analysis["categories"][m]:
                    row.append(h["per10"].get(str(c["category"]), "") if m == mode else "")
            heat.append(row)
    return {"Успеваемость": perf, "Прогноз и факт": check, "Ошибки на 10 карточек": heat}
