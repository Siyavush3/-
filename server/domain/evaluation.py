"""Оценка работы диспетчера ДДС по карточке (ARCHITECTURE.md §8.4).

Каждое нарушение относится к одной из семи категорий памятки ДДС и называется
словами памятки — преподаватель видит, за что снят балл, а не число.
Эталон — поле ``expect`` карточки сценария; доклады бригады — то, что реально
было доставлено обучающемуся (с серверным временем).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from arm112.paths import data_dir

SYSTEM = {"Добавлена", "Получена службой"}
COMMENT_REQUIRED = {"Не принята", "Отказ от выполнения работ"}
TERMINAL = {"Работы завершены", "Отказ от выполнения работ"}
PROGRESS = {"Начало реагирования", "Прибытие", "Проведение работ", "Работы завершены"}
TEXT_CATEGORY = 8  # ошибки в тексте комментария (узел 3)


@dataclass(frozen=True)
class Step:
    status: str
    at: float
    comment: str = ""


@dataclass(frozen=True)
class Report:
    """Доклад старшего бригады, доставленный обучающемуся."""
    at: float
    status: str   # статус, который из доклада следует
    text: str


@dataclass(frozen=True)
class Finding:
    category: int
    detail: str
    card: str = ""     # номер карточки
    status: str = ""


@dataclass
class Rubric:
    categories: Dict[int, dict]
    primary_norm_s: float = 30.0
    progress_window_s: float = 60.0
    text_cap: int = 5

    @classmethod
    def load(cls, path=None) -> "Rubric":
        with open(path or data_dir() / "evaluation" / "rubric.json", encoding="utf-8") as f:
            raw = json.load(f)
        return cls({int(k): v for k, v in raw["categories"].items()},
                   float(raw["primary_status_norm_s"]), float(raw["progress_window_s"]), int(raw.get("text_cap", 5)))

    def title(self, category: int) -> str:
        return self.categories[category]["title"]

    def score(self, findings: Sequence[Finding]) -> int:
        """100 минус штрафы; ошибки в тексте (категория 8) считаются не больше text_cap раз."""
        total, text = 0, 0
        for f in findings:
            if f.category == TEXT_CATEGORY:
                text += 1
                if text > self.text_cap:
                    continue
            total += self.categories[f.category]["weight"]
        return max(0, 100 - total)


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def missing_facts(comment: str, groups: Optional[List[List[str]]], numbers: Dict[str, str],
                  checker=None) -> List[str]:
    """Группы фактов, которых нет в комментарии. В группе достаточно любого варианта.
    Вариант «{A}» — номер карточки A в этом занятии. С checker (domain.textcheck) варианты
    сравниваются и по словоформам: «передали» засчитывается за «передан»."""
    text = _norm(comment)
    missing = []
    for group in groups or []:
        variants = [numbers.get(v[1:-1], v) if v.startswith("{") and v.endswith("}") else v for v in group]
        if any(_norm(v) in text for v in variants):
            continue
        if checker is not None and not checker.missing(comment, [variants]):
            continue
        missing.append(variants[0])
    return missing


def text_findings(comment: str, checker, card: str = "", status: str = "") -> List[Finding]:
    """Орфография и оформление комментария — категория 8."""
    if checker is None or not comment.strip():
        return []
    out = []
    for err in checker.spelling(comment):
        hint = ", ".join(err["suggestions"]) or "нет в словаре"
        out.append(Finding(TEXT_CATEGORY, "«%s» → %s" % (err["word"], hint), card, status))
    for err in checker.grammar(comment):
        out.append(Finding(TEXT_CATEGORY, "%s: «%s»" % (err["detail"], err["fragment"]), card, status))
    return out


def evaluate_card(expect: dict, added_at: float, steps: Sequence[Step], reports: Sequence[Report],
                  numbers: Dict[str, str], rubric: Rubric, card: str = "", checker=None) -> List[Finding]:
    """Нарушения по одной карточке. steps — ручные статусы обучающегося по порядку."""
    out: List[Finding] = []
    manual = [s for s in steps if s.status not in SYSTEM]

    def add(cat, detail, status=""):
        out.append(Finding(cat, detail, card, status))

    # Первичный статус: срок и решение. Решение оценивается по первому статусу —
    # именно его видят другие службы и отдел контроля.
    primary = manual[0] if manual else None
    if primary is None:
        add(1, "статус реагирования не проставлен")
    else:
        delay = primary.at - added_at
        if delay > rubric.primary_norm_s:
            add(1, "первичный статус через %.0f с при нормативе %.0f с" % (delay, rubric.primary_norm_s), primary.status)
        if expect["primary"] == "Не принята" and primary.status == "Принята":
            add(2, "следовало проставить «Не принята» с комментарием", primary.status)
        elif expect["primary"] == "Принята" and primary.status == "Не принята":
            add(3, "происшествие профильное — следовало «Принята»", primary.status)

    # Комментарии к отказным статусам: есть ли и полон ли.
    for step in manual:
        if step.status not in COMMENT_REQUIRED:
            continue
        if not step.comment.strip():
            add(4, "«%s» без комментария" % step.status, step.status)
            continue
        facts = expect.get("comment_facts") if step.status == "Не принята" else expect.get("final_comment_facts")
        lack = missing_facts(step.comment, facts, numbers, checker)
        if lack:
            add(5, "в комментарии не указано: %s" % ", ".join(lack), step.status)

    # Ход работ: по каждому докладу бригады — свой статус в окне, с комментарием.
    for rep in reports:
        hits = [s for s in manual if s.status == rep.status]
        if not hits:
            add(6, "после доклада «%s» не проставлен статус «%s»" % (rep.text, rep.status), rep.status)
            continue
        first = hits[0]
        if first.at < rep.at:
            add(2, "«%s» проставлен до доклада бригады" % rep.status, rep.status)
        elif first.at - rep.at > rubric.progress_window_s:
            add(6, "«%s» проставлен через %.0f с после доклада" % (rep.status, first.at - rep.at), rep.status)
    for step in manual:
        if step.status in PROGRESS and not step.comment.strip():
            add(6, "статус «%s» без комментария" % step.status, step.status)

    # Итоговый статус должен совпасть с фактическим исходом работ.
    final_expected = expect.get("final")
    finals = [s for s in manual if s.status in TERMINAL]
    if final_expected and finals and finals[-1].status != final_expected:
        add(2, "итог работ — «%s», проставлен «%s»" % (final_expected, finals[-1].status), finals[-1].status)

    for step in manual:
        out.extend(text_findings(step.comment, checker, card, step.status))
    return out


@dataclass
class ParticipantResult:
    participant_id: str
    role: str
    findings: List[Finding] = field(default_factory=list)
    score: int = 100

    def to_dict(self, rubric: Rubric) -> dict:
        by_cat: Dict[int, int] = {}
        for f in self.findings:
            by_cat[f.category] = by_cat.get(f.category, 0) + 1
        return {
            "participant": self.participant_id,
            "role": self.role,
            "score": self.score,
            "by_category": [{"category": c, "title": rubric.title(c), "count": n} for c, n in sorted(by_cat.items())],
            "findings": [{"category": f.category, "title": rubric.title(f.category), "card": f.card,
                          "status": f.status, "detail": f.detail} for f in self.findings],
        }


# ---------- критерии успешности занятия (узел 6) ----------
DEFAULT_CRITERIA = {"pass_score": 70, "max_errors": None, "text_check": True}


def criteria_of(config: Optional[dict]) -> dict:
    """Критерии из параметров занятия; чего преподаватель не задал — по умолчанию (допущение 6.2)."""
    out = dict(DEFAULT_CRITERIA)
    for k in DEFAULT_CRITERIA:
        if (config or {}).get(k) is not None:
            out[k] = (config or {})[k]
    return out


def verdict(score: int, errors: int, criteria: dict) -> dict:
    """Зачёт: балл не ниже порога и ошибок не больше допустимого. Причины незачёта — словами."""
    reasons = []
    if score < criteria["pass_score"]:
        reasons.append("балл %d ниже порога %d" % (score, criteria["pass_score"]))
    if criteria.get("max_errors") is not None and errors > criteria["max_errors"]:
        reasons.append("замечаний %d при допустимых %d" % (errors, criteria["max_errors"]))
    return {"passed": not reasons, "reasons": reasons}
