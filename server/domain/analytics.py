"""Аналитика обучения (узел 6): прогноз результата, проверка прогноза на истории, тепловая карта ошибок,
инсайты по типичным ошибкам группы и рекомендации.

Всё объяснимо и воспроизводимо. ТЗ, экспертиза: «проверка прогнозирования, оценка достоверности путём
сравнения реальных данных и прогнозных значений», «соответствие рекомендаций реальным рекомендациям».
- Прогноз следующего результата — сглаживание Холта с затуханием тренда; начальный тренд — средний
  прирост группы от занятия к занятию.
- Достоверность — пошаговая проверка на прошлых занятиях: каждое занятие предсказывается только по
  предыдущим и сравнивается с фактом; для сравнения — наивный прогноз «как в прошлый раз».
- Рекомендации — из памятки ДДС и инструкции оператора 112 по категориям ошибок, с готовыми
  параметрами тренировки для генератора сценариев.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

# Сглаживание уровня, тренда и затухание тренда; K_SHRINK — насколько начальный тренд опирается на группу,
# а не на собственный прирост обучающегося (вес собственного — (n−1)/(n−1+K)). Подобраны на синтетических
# кривых обучения (600 обучающихся × 8 занятий, приближение к потолку, шум 3–8 баллов): средняя ошибка
# 6,2 балла против 7,2 у прогноза «как в прошлый раз»; из почти равных по точности (±2%) вариантов взят тот,
# что быстрее откликается на явный личный рост. Проверка — tests/test_teaching.py.
ALPHA, BETA, PHI, K_SHRINK = 0.5, 0.2, 0.9, 2.0
DEFAULT_SIGMA = 12.0                 # разброс прогноза, пока истории мало (баллов)
Z80 = 1.2816                         # 80% интервал прогноза
MODES = {"действия с карточками": "ДДС", "карточки": "112"}

RECS: Dict[Tuple[str, int], dict] = {
    ("ДДС", 1): {"text": "Первичный статус «Принята» или «Не принята» — в течение норматива (по умолчанию 30 секунд) "
                         "после поступления карточки (памятка ДДС). Тренировка на скорость: карточки приходят чаще.",
                 "preset": {"mode": "card_actions", "difficulty": 3}},
    ("ДДС", 2): {"text": "Перед решением сверять адрес со справкой службы: дом другой УК, лифт другой организации, "
                         "территория ГБУ «Автодороги», электрощитовая ОЭК — «Не принята»; повторное сообщение — дубль "
                         "со ссылкой на первую карточку. Тренировка: сценарий с ловушками памятки.",
                 "preset": {"mode": "card_actions", "difficulty": 2}},
    ("ДДС", 3): {"text": "Профильное происшествие в своём жилом фонде — «Принята» и ход работ, отказ недопустим. "
                         "Тренировка: сценарий базовой сложности — различать профильные карточки.",
                 "preset": {"mode": "card_actions", "difficulty": 1}},
    ("ДДС", 4): {"text": "«Не принята» и «Отказ от выполнения работ» — всегда с комментарием: причина и куда передана "
                         "информация (памятка ДДС). Опираться на образцы комментариев в разборе.",
                 "preset": {"mode": "card_actions", "difficulty": 2}},
    ("ДДС", 5): {"text": "В комментарии — обе части: почему не принята (кто обслуживает дом, лифт, территорию) и кому "
                         "передано (организация, телефон диспетчерской).",
                 "preset": {"mode": "card_actions", "difficulty": 2}},
    ("ДДС", 6): {"text": "Статусы «Начало реагирования», «Прибытие», «Проведение работ», «Работы завершены» — по каждому "
                         "докладу старшего бригады, не позже 60 секунд, с комментарием.",
                 "preset": {"mode": "card_actions", "difficulty": 1}},
    ("ДДС", 7): {"text": "Не закрывать АРМ во время занятия; при потере связи — сообщить преподавателю и проверить "
                         "подключение рабочего места.", "preset": None},
    ("ДДС", 8): {"text": "Перечитывать комментарий перед сохранением: без опечаток, двойных пробелов и лишних пробелов "
                         "перед знаками.", "preset": None},
    ("112", 1): {"text": "Тип происшествия — через «Что случилось?»: поиск по части слова и опросная карта до конца "
                         "(инструкция пользователя, раздел 4).", "preset": {"mode": "cards", "difficulty": 1}},
    ("112", 2): {"text": "Уточнять признаки опросной карты по ответам заявителя: каждый уровень — отдельный вопрос.",
                 "preset": {"mode": "cards", "difficulty": 1}},
    ("112", 3): {"text": "Задавать вопросы опросной карты «да/нет»: угроза людям, газификация, пострадавшие.",
                 "preset": {"mode": "cards", "difficulty": 2}},
    ("112", 4): {"text": "Если заявитель назвал ориентир — переспросить улицу и дом; сверять адрес перед сохранением. "
                         "Тренировка: заявитель сначала называет только ориентир.",
                 "preset": {"mode": "cards", "difficulty": 2}},
    ("112", 5): {"text": "Заполнять подъезд, этаж, квартиру и код домофона — по ним едут службы.",
                 "preset": {"mode": "cards", "difficulty": 2}},
    ("112", 6): {"text": "Обязательные поля — ФИО и статус заявителя, пострадавшие, описание (инструкция, раздел 5).",
                 "preset": {"mode": "cards", "difficulty": 1}},
    ("112", 7): {"text": "Главное — в первых 100 символах описания: они уходят в службу 03; перечислять ключевые факты.",
                 "preset": {"mode": "cards", "difficulty": 3}},
    ("112", 8): {"text": "Перечитывать описание перед сохранением: без опечаток и лишних пробелов.", "preset": None},
    ("112", 9): {"text": "Заполнять карточку в норматив: сначала адрес и тип, затем подробности. Тренировка на скорость — "
                         "вызовы базовой сложности.", "preset": {"mode": "cards", "difficulty": 1}},
    ("112", 10): {"text": "Не менять список служб без причины: службы подбираются по классификатору (ЕКП).",
                  "preset": None},
    ("112", 11): {"text": "Принимать вызов сразу (Enter), не позднее норматива (15 секунд).", "preset": None},
    ("112", 12): {"text": "Не оставлять вызов необработанным: сохранить карточку до конца занятия.",
                  "preset": {"mode": "cards", "difficulty": 1}},
}


def final_score(row: dict) -> int:
    """Итоговый балл: экспертная оценка преподавателя, если есть, иначе автоматическая."""
    return int(row["expert_score"] if row.get("expert_score") is not None else row["score"])


def clip(x: float) -> float:
    return max(0.0, min(100.0, x))


def holt(ys: Sequence[float], start_trend: float = 0.0) -> Tuple[float, float]:
    """Уровень и тренд после ряда ys: сглаживание Холта с затуханием тренда."""
    level, trend = float(ys[0]), float(start_trend)
    for y in ys[1:]:
        prev = level
        level = ALPHA * y + (1 - ALPHA) * (level + PHI * trend)
        trend = BETA * (level - prev) + (1 - BETA) * PHI * trend
    return level, trend


def initial_trend(ys: Sequence[float], group_step: float) -> float:
    """Начальный тренд: собственный средний прирост, «сжатый» к приросту группы, — чем меньше занятий, тем ближе к группе."""
    n = len(ys)
    if n < 2:
        return group_step
    w = (n - 1) / (n - 1 + K_SHRINK)
    return w * (ys[-1] - ys[0]) / (n - 1) + (1 - w) * group_step


def predict(ys: Sequence[float], group_step: float = 0.0) -> Optional[float]:
    """Прогноз следующего результата только по предыдущим. Одно занятие — прирост как в среднем по группе."""
    if not ys:
        return None
    level, trend = holt(ys, initial_trend(ys, group_step))
    return clip(level + PHI * trend)


def group_step(series: Dict[int, List[float]], exclude: Optional[int] = None) -> float:
    """Средний прирост от занятия к занятию по группе (без самого обучающегося — чтобы не подглядывать)."""
    steps = [b - a for sid, ys in series.items() if sid != exclude for a, b in zip(ys, ys[1:])]
    return max(-5.0, min(10.0, sum(steps) / len(steps))) if steps else 0.0


def backtest(ys: Sequence[float], step: float) -> List[dict]:
    """Пошаговая проверка: занятие i предсказано по занятиям до него; рядом — наивный прогноз «как в прошлый раз»."""
    return [{"index": i, "predicted": round(predict(ys[:i], step), 1), "actual": ys[i], "naive": ys[i - 1]}
            for i in range(1, len(ys))]


def metrics(points: Sequence[dict], threshold: float) -> dict:
    if not points:
        return {"n": 0}
    err = [p["actual"] - p["predicted"] for p in points]
    naive = [p["actual"] - p["naive"] for p in points]
    agree = sum((p["predicted"] >= threshold) == (p["actual"] >= threshold) for p in points)
    return {"n": len(points), "mae": round(sum(map(abs, err)) / len(err), 1),
            "rmse": round(math.sqrt(sum(e * e for e in err) / len(err)), 1),
            "bias": round(sum(err) / len(err), 1), "mae_naive": round(sum(map(abs, naive)) / len(naive), 1),
            "pass_agreement": round(agree / len(points), 2)}


def normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def forecast(ys: Sequence[float], step: float, sigma: float, threshold: float) -> Optional[dict]:
    if not ys:
        return None
    level, trend = holt(ys, initial_trend(ys, step))
    nxt = clip(level + PHI * trend)
    per_lesson = PHI * trend
    to_pass = None
    if nxt >= threshold:
        to_pass = 1
    elif per_lesson > 0.5:
        to_pass = min(20, 1 + math.ceil((threshold - nxt) / per_lesson))
    return {"next": round(nxt), "low": round(clip(nxt - Z80 * sigma)), "high": round(clip(nxt + Z80 * sigma)),
            "sigma": round(sigma, 1), "trend": round(per_lesson, 1), "basis": len(ys),
            "p_pass": round(1 - normal_cdf((threshold - nxt) / max(sigma, 1.0)), 2), "lessons_to_pass": to_pass}


# ---------- ошибки ----------
def mode_of(row: dict) -> str:
    return MODES.get(row.get("lesson_mode") or "", row.get("lesson_mode") or "")


def error_counts(rows: Sequence[dict]) -> Tuple[Counter, int]:
    counts: Counter = Counter()
    cards = 0
    for r in rows:
        cards += int((r.get("summary") or {}).get("cards") or 0) or 1
        for f in r.get("findings") or []:
            counts[int(f["category"])] += 1
    return counts, cards


def misspellings(rows: Sequence[dict]) -> Counter:
    words: Counter = Counter()
    for r in rows:
        for f in r.get("findings") or []:
            if int(f["category"]) == 8:
                m = re.match(r"«([^»]+)»\s*→", f.get("detail") or "")
                if m:
                    words[m.group(1).lower()] += 1
    return words


# ---------- сводка по группе ----------
def analyze(rows: Sequence[dict], students: Sequence[dict], categories: Dict[str, List[dict]],
            threshold: float = 70.0) -> dict:
    """rows — результаты (repos.Results._rows), students — [{id, name}], categories — {режим: [{category, title}]}."""
    names = {s["id"]: s["name"] for s in students}
    by_student: Dict[int, List[dict]] = defaultdict(list)
    for r in sorted(rows, key=lambda r: (r.get("finished_at") or r.get("created_at") or 0, r["id"])):
        if r["user_id"] in names:
            by_student[r["user_id"]].append(r)
    series = {sid: [float(final_score(r)) for r in rs] for sid, rs in by_student.items()}
    pooled: List[dict] = []
    per_student = []
    for s in students:
        rs = by_student.get(s["id"], [])
        ys = series.get(s["id"], [])
        step = group_step(series, exclude=s["id"])
        bt = backtest(ys, step)
        pooled += bt
        per_student.append((s, rs, ys, step, bt))
    group_rmse = metrics(pooled, threshold).get("rmse") if len(pooled) >= 5 else None
    out_students = []
    for s, rs, ys, step, bt in per_student:
        own = metrics(bt, threshold)
        sigma = own["rmse"] if own.get("n", 0) >= 3 else (group_rmse or DEFAULT_SIGMA)
        history = []
        for i, r in enumerate(rs):
            pred = next((p["predicted"] for p in bt if p["index"] == i), None)
            summary = r.get("summary") or {}
            history.append({"result_id": r["id"], "lesson": "L%d" % r["lesson_id"], "title": r.get("lesson_title"),
                            "mode": mode_of(r), "finished_at": r.get("finished_at"), "score": final_score(r),
                            "auto": r["score"], "expert": r.get("expert_score"), "predicted": pred,
                            "passed": final_score(r) >= (summary.get("criteria") or {}).get("pass_score", threshold),
                            "cards": summary.get("cards"), "errors": len(r.get("findings") or [])})
        heat = {}
        for mode in categories:
            mine = [r for r in rs if mode_of(r) == mode]
            counts, cards = error_counts(mine)
            if mine:
                heat[mode] = {"cards": cards, "counts": {str(c): counts.get(c, 0) for c in (x["category"] for x in categories[mode])},
                              "per10": {str(c): round(10 * counts.get(c, 0) / cards, 2) for c in (x["category"] for x in categories[mode])}}
        out_students.append({"id": s["id"], "name": s["name"], "history": history, "backtest": own,
                             "forecast": forecast(ys, step, sigma, threshold), "heat": heat,
                             "recommendations": student_recommendations(rs, categories)})
    return {"threshold": threshold, "students": out_students, "categories": categories,
            "backtest": metrics(pooled, threshold), "insights": insights(out_students, by_student, categories, threshold),
            "recommendations": group_recommendations(by_student, categories)}


def _typical(by_student: Dict[int, List[dict]], categories: Dict[str, List[dict]]) -> List[dict]:
    """Категории ошибок, у скольких обучающихся встречаются и как часто; тренд — первая половина занятий против второй."""
    out = []
    for mode, cats in categories.items():
        students = {sid: [r for r in rs if mode_of(r) == mode] for sid, rs in by_student.items()}
        students = {sid: rs for sid, rs in students.items() if rs}
        if not students:
            continue
        for cat in cats:
            c = int(cat["category"])
            with_err = sum(1 for rs in students.values() if any(int(f["category"]) == c for r in rs for f in r["findings"] or []))
            if not with_err:
                continue
            all_rows = sorted((r for rs in students.values() for r in rs), key=lambda r: (r.get("finished_at") or 0, r["id"]))
            half = len(all_rows) // 2
            (early, early_cards), (late, late_cards) = error_counts(all_rows[:half]), error_counts(all_rows[half:])
            total, cards = error_counts(all_rows)
            trend = None
            if half and early_cards and late_cards:
                a, b = early[c] / early_cards, late[c] / late_cards
                trend = "реже" if b < a * 0.8 else "чаще" if b > a * 1.2 and b - a > 0.02 else "без изменений"
            out.append({"mode": mode, "category": c, "title": cat["title"], "students": with_err,
                        "of": len(students), "per10": round(10 * total[c] / cards, 2), "trend": trend})
    return sorted(out, key=lambda x: (-x["students"] / x["of"], -x["per10"]))


def insights(students: List[dict], by_student: Dict[int, List[dict]], categories: Dict[str, List[dict]],
             threshold: float) -> List[dict]:
    out = []
    for t in _typical(by_student, categories)[:3]:
        if t["students"] / t["of"] < 0.3:
            continue
        trend = {"реже": ", за последние занятия — реже", "чаще": ", за последние занятия — чаще",
                 "без изменений": ", частота не меняется"}.get(t["trend"] or "", "")
        out.append({"kind": "typical", "severity": "high" if t["students"] / t["of"] >= 0.6 else "medium",
                    "mode": t["mode"], "category": t["category"],
                    "text": "Типичная ошибка (%s): «%s» — у %d из %d обучающихся, %s на 10 карточек%s."
                            % (t["mode"], t["title"], t["students"], t["of"], _num(t["per10"]), trend)})
    risk = [s for s in students if s["forecast"] and s["forecast"]["p_pass"] < 0.5]
    if risk:
        out.append({"kind": "risk", "severity": "high",
                    "text": "Прогноз ниже порога зачёта (%d баллов): %s." % (threshold, "; ".join(
                        "%s — прогноз %d, вероятность зачёта %d%%" % (s["name"], s["forecast"]["next"], round(100 * s["forecast"]["p_pass"]))
                        for s in sorted(risk, key=lambda s: s["forecast"]["p_pass"])[:5]))})
    falling = [s for s in students if s["forecast"] and s["forecast"]["basis"] >= 3 and s["forecast"]["trend"] <= -3]
    if falling:
        out.append({"kind": "decline", "severity": "medium",
                    "text": "Результаты снижаются: %s." % ", ".join("%s (%s балла за занятие)" % (s["name"], _num(s["forecast"]["trend"]))
                                                                   for s in falling)})
    grown = [s for s in students if len(s["history"]) >= 2 and s["history"][-1]["score"] - s["history"][0]["score"] >= 15]
    if grown:
        out.append({"kind": "progress", "severity": "low",
                    "text": "Заметный рост: %s." % ", ".join("%s (%d → %d)" % (s["name"], s["history"][0]["score"], s["history"][-1]["score"])
                                                             for s in grown)})
    slow = []
    for s in students:
        rs = [r for r in by_student.get(s["id"], []) if mode_of(r) == "112"]
        over = sum(((r.get("summary") or {}).get("timing") or {}).get("over_norm") or 0 for r in rs)
        cards = sum(int((r.get("summary") or {}).get("cards") or 0) for r in rs)
        if cards and over / cards > 0.3:
            slow.append(s["name"])
    if slow:
        out.append({"kind": "timing", "severity": "medium",
                    "text": "Карточки 112 дольше норматива (больше трети карточек): %s." % ", ".join(slow)})
    words = misspellings([r for rs in by_student.values() for r in rs])
    if words:
        out.append({"kind": "spelling", "severity": "low",
                    "text": "Частые ошибки в тексте: %s." % ", ".join("«%s» (%d)" % (w, n) for w, n in words.most_common(5))})
    return out


def group_recommendations(by_student: Dict[int, List[dict]], categories: Dict[str, List[dict]]) -> List[dict]:
    out = []
    for t in _typical(by_student, categories)[:4]:
        rec = RECS.get((t["mode"], t["category"]))
        if rec:
            out.append({"mode": t["mode"], "category": t["category"], "title": t["title"], "text": rec["text"],
                        "preset": rec["preset"], "students": t["students"], "of": t["of"]})
    return out


def student_recommendations(rows: Sequence[dict], categories: Dict[str, List[dict]], last: int = 3) -> List[dict]:
    """По последним занятиям каждого режима: самые частые категории ошибок и что с ними делать."""
    out = []
    titles = {(m, int(c["category"])): c["title"] for m, cats in categories.items() for c in cats}
    for mode in categories:
        recent = [r for r in rows if mode_of(r) == mode][-last:]
        counts, cards = error_counts(recent)
        for cat, n in counts.most_common(3):
            rec = RECS.get((mode, cat))
            if rec:
                out.append({"mode": mode, "category": cat, "title": titles.get((mode, cat), str(cat)), "count": n,
                            "per10": round(10 * n / cards, 1), "text": rec["text"], "preset": rec["preset"]})
    return out


def _num(x: float) -> str:
    return ("%.1f" % x).replace(".", ",")
