"""Ход занятия в режиме «карточки»: у каждого обучающегося своя очередь вызовов.

Звонок → «принять» → разговор с заявителем-ботом → карточка → сохранение → оценка → следующий звонок.
Время — серверное (часы движка); нормативы: принять вызов (answer_norm_s) и заполнить карточку (norm_s).
Класс не знает о транспорте: методы возвращают [(адресат, сообщение)], как и движок.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from domain.calls import Caller, CallPack, CardTiming, card_score, evaluate_card
from domain.evaluation import Finding

Out = Tuple[str, dict]
RING_DELAY_S = 3.0      # пауза перед первым звонком
NEXT_CALL_S = 5.0       # пауза между карточками
DEFAULT_CALLS = 8


@dataclass
class CallState:
    key: str
    number: str
    rang_at: float
    caller: Caller
    answered_at: Optional[float] = None
    transcript: List[dict] = field(default_factory=list)


@dataclass
class DoneCard:
    key: str
    number: str
    score: int
    findings: List[Finding]
    fill_s: Optional[float]
    answer_s: Optional[float]
    card: dict
    code: Optional[str]


class CallDesk:
    def __init__(self, pack: CallPack, classifier, router, rubric: dict, lesson_no: int, seed: str,
                 wall: Callable[[float], float], count: int = DEFAULT_CALLS, feedback: str = "immediate",
                 norm_s: Optional[float] = None, groups: Optional[List[str]] = None, checker=None, speech=None,
                 answer_norm_s: Optional[float] = None):
        self.pack, self.classifier, self.router, self.rubric = pack, classifier, router, rubric
        self.wall = wall
        self.speech = speech  # ссылки на озвученные реплики заявителя (узел 5)
        self.feedback = feedback
        self.norm_s = float(norm_s or pack.norm_s)
        self.answer_norm_s = float(answer_norm_s or pack.answer_norm_s)
        self.checker = checker
        calls = [c for c in pack.calls
                 if not groups or classifier.incidents[c["gold"]["codes"][0]]["group"] in groups]
        rng = random.Random(seed)
        rng.shuffle(calls)
        self.keys = [c["key"] for c in calls[:max(1, count)]]
        self.numbers = {k: str(36900000 + lesson_no * 1000 + 101 + i) for i, k in enumerate(self.keys)}
        self.by_number = {n: k for k, n in self.numbers.items()}
        self.queue: Dict[str, List[str]] = {}
        self.current: Dict[str, Optional[CallState]] = {}
        self.done: Dict[str, List[DoneCard]] = {}
        self.last_seq: Dict[str, int] = {}

    # ---------- жизненный цикл ----------
    def start(self, pids: List[str], now: float, schedule) -> None:
        for pid in pids:
            self.queue[pid] = list(self.keys)
            self.current[pid] = None
            self.done[pid] = []
            schedule(now + RING_DELAY_S, "ring", (pid,))

    def ring(self, pid: str, now: float) -> List[Out]:
        if self.current.get(pid) is not None or not self.queue.get(pid):
            return []
        key = self.queue[pid].pop(0)
        call = self.pack.by_key[key]
        state = CallState(key, self.numbers[key], now, Caller(call))
        self.current[pid] = state
        return [(pid, {"t": "incoming", "call": self._public_call(state)})]

    # ---------- действия обучающегося ----------
    def handle(self, pid: str, msg: dict, now: float, schedule, log) -> List[Out]:
        kind = msg.get("t")
        state = self.current.get(pid)
        if kind == "answer":
            if state is None or state.answered_at is not None:
                return []
            state.answered_at = now
            log("answer", participant=pid, card=state.number)
            return [(pid, self._say(state, state.caller.call["dialogue"]["opening"], now))]
        if kind == "ask":
            if state is None or state.answered_at is None:
                return [(pid, {"t": "error", "reason": "сначала примите вызов"})]
            question = str(msg.get("text") or "")[:300]
            intent = msg.get("intent") or None
            from domain.calls import detect_intent
            intent = intent if intent in state.caller.call["dialogue"] else detect_intent(question)
            line = {"who": "оператор", "text": question or intent or "", "at": self.wall(now)}
            voice = msg.get("via") == "voice"
            if voice:
                line["via"] = "voice"  # вопрос сказан голосом и распознан (узел 9)
            state.transcript.append(line)
            log("ask", participant=pid, card=state.number, intent=intent, text=question, **({"via": "voice"} if voice else {}))
            return [(pid, self._say(state, state.caller.answer(intent), now))]
        if kind == "say":  # реплика оператора голосом без вопроса («хорошо, оставайтесь на линии») — заявитель молчит
            text = str(msg.get("text") or "")[:300]
            if state is None or state.answered_at is None or not text:
                return []
            state.transcript.append({"who": "оператор", "text": text, "at": self.wall(now), "via": "voice"})
            log("say", participant=pid, card=state.number, text=text)
            return []
        if kind == "save_card":
            return self._save(pid, msg, now, schedule, log)
        return [(pid, {"t": "error", "reason": "неизвестное сообщение"})]

    def _save(self, pid: str, msg: dict, now: float, schedule, log) -> List[Out]:
        seq = int(msg.get("seq", 0) or 0)
        if seq and seq <= self.last_seq.get(pid, 0):
            return [(pid, {"t": "save_ack", "seq": seq, "duplicate": True})]
        state = self.current.get(pid)
        card = msg.get("card") if isinstance(msg.get("card"), dict) else {}
        if state is None or state.answered_at is None or str(card.get("number")) != state.number:
            return [(pid, {"t": "save_rejected", "seq": seq, "reason": "нет открытой карточки с таким номером"})]
        if seq:
            self.last_seq[pid] = seq
        done = self._evaluate(state, card, now)
        self.done[pid].append(done)
        self.current[pid] = None
        log("save", participant=pid, card=state.number, score=done.score, code=done.code,
            findings=[f.category for f in done.findings])
        outs = [(pid, {"t": "save_ack", "seq": seq}), (pid, {"t": "card_result", "result": self._public_done(done)})]
        if self.queue.get(pid):
            schedule(now + NEXT_CALL_S, "ring", (pid,))
        else:
            outs.append((pid, {"t": "queue_done"}))
        return outs

    def _evaluate(self, state: CallState, card: dict, now: float) -> DoneCard:
        call = self.pack.by_key[state.key]
        code = card.get("code") if card.get("code") in self.classifier.incidents else None
        flags = [f for f in (card.get("flags") or []) if f in self.classifier.flags]
        if card.get("victims") is True:
            flags.append("ПП")
        auto = [p.name for p in self.router.plaques(code, flags, None) if p.row == 1] if code else []
        card = dict(card, number=state.number)
        findings = evaluate_card(call, card, CardTiming(state.rang_at, state.answered_at, now), self.classifier,
                                 self.rubric, auto, self.norm_s, self.answer_norm_s, checker=self.checker)
        return DoneCard(state.key, state.number, card_score(findings, self.rubric), findings,
                        now - state.answered_at, state.answered_at - state.rang_at, card, code)

    # ---------- итог ----------
    def saved_cards(self) -> List[dict]:
        """Сохранённые карточки с оценкой — их получат обучающиеся ДДС (ТЗ, сценарий 2)."""
        return [{"participant": pid, "number": d.number, "code": d.code, "score": d.score,
                 "categories": sorted({f.category for f in d.findings}), "card": d.card}
                for pid, cards in self.done.items() for d in cards if d.code]
    def results(self, pid: str, now: float) -> dict:
        cards = list(self.done.get(pid, []))
        state = self.current.get(pid)
        if state is not None:  # занятие закончилось посреди вызова — карточка не обработана
            reason = "вызов не принят" if state.answered_at is None else "карточка не сохранена"
            cards.append(DoneCard(state.key, state.number, 0, [Finding(12, reason, card=state.number)],
                                  None, None if state.answered_at is None else state.answered_at - state.rang_at, {}, None))
        findings = [f for c in cards for f in c.findings]
        fills = [c.fill_s for c in cards if c.fill_s is not None]
        cats = {int(k): v for k, v in self.rubric["categories"].items()}
        by_cat: Dict[int, int] = {}
        for f in findings:
            by_cat[f.category] = by_cat.get(f.category, 0) + 1
        return {
            "participant": pid, "role": "Оператор 112",
            "score": round(sum(c.score for c in cards) / len(cards)) if cards else 0,
            "by_category": [{"category": c, "title": cats[c]["title"], "count": n} for c, n in sorted(by_cat.items())],
            "findings": [{"category": f.category, "title": cats[f.category]["title"], "card": f.card,
                          "status": None, "detail": f.detail} for f in findings],
            "cards": len(cards),
            "categories": [{"category": c, "title": v["title"], "weight": v["weight"]} for c, v in sorted(cats.items())],
            "timing": {"avg_fill_s": round(sum(fills) / len(fills), 1) if fills else None,
                       "max_fill_s": round(max(fills), 1) if fills else None, "norm_s": self.norm_s,
                       "over_norm": sum(1 for x in fills if x > self.norm_s)},
            "cards_detail": [self._public_done(c, gold=True) for c in cards],
        }

    # ---------- представления ----------
    def _public_call(self, state: CallState) -> dict:
        caller = state.caller.call["caller"]
        return {"number": state.number, "phone": caller["phone"], "rang_at": self.wall(state.rang_at),
                "answered_at": self.wall(state.answered_at) if state.answered_at is not None else None,
                "transcript": state.transcript, "voice": caller.get("gender", "m")}

    def _say(self, state: CallState, text: str, now: float) -> dict:
        line = {"who": "заявитель", "text": text, "at": self.wall(now)}
        audio = self.speech.caller_audio(state.caller.call, text) if self.speech else None
        if audio:
            line["audio"] = audio
        state.transcript.append(line)
        return {"t": "caller_say", "call": state.number, "line": line}

    def _public_done(self, done: DoneCard, gold: Optional[bool] = None) -> dict:
        cats = self.rubric["categories"]
        out = {"number": done.number, "score": done.score, "fill_s": round(done.fill_s, 1) if done.fill_s is not None else None,
               "answer_s": round(done.answer_s, 1) if done.answer_s is not None else None,
               "findings": [{"category": f.category, "title": cats[str(f.category)]["title"], "detail": f.detail}
                            for f in done.findings]}
        if gold if gold is not None else self.feedback == "immediate":
            g = self.pack.by_key[done.key]["gold"]
            inc = self.classifier.incidents[g["codes"][0]]
            out["gold"] = {"type": inc["type"], "code": g["codes"][0], "address": g["address"], "flags": g["flags"],
                           "victims": g["victims"], "situation": self.pack.by_key[done.key]["situation"]}
        return out

    def snapshot(self, pid: str) -> dict:
        state = self.current.get(pid)
        return {"current": self._public_call(state) if state else None,
                "done": [self._public_done(d) for d in self.done.get(pid, [])],
                "total": len(self.keys), "remaining": len(self.queue.get(pid, [])) + (1 if state else 0),
                "norm_s": self.norm_s, "answer_norm_s": self.answer_norm_s, "feedback": self.feedback}

    def monitor_row(self, pid: str, now: float) -> dict:
        state = self.current.get(pid)
        done = self.done.get(pid, [])
        fills = [d.fill_s for d in done if d.fill_s is not None]
        talking = state is not None and state.answered_at is not None
        return {"calls_done": len(done), "calls_total": len(self.keys),
                "call_state": "разговор" if talking else "звонок" if state else "ожидание",
                "avg_fill_s": round(sum(fills) / len(fills)) if fills else None,
                "avg_score": round(sum(d.score for d in done) / len(done)) if done else None,
                "overdue": int(talking and now - state.answered_at > self.norm_s)
                           + int(state is not None and not talking and now - state.rang_at > self.answer_norm_s)}
