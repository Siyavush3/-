"""Движок занятий: лобби + сценарий + маршрутизация + автомат статусов + оценка (ARCHITECTURE.md §18).

Однопоточный и детерминированный. Всё время — серверное (``clock``), отображаемое — ``wall``.
События обрабатываются в ``tick()`` и ``handle()``; наружу движок только возвращает сообщения
[(адресат, сообщение)] — транспорт (WebSocket) и хранилище подключает прикладной слой.
Адресат — идентификатор участника или ``teacher:<id>`` для монитора преподавателя.
"""
from __future__ import annotations

import dataclasses
import heapq
import itertools
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from arm112.reference.fsm import ResponseFsm
from domain.evaluation import (TERMINAL, Finding, ParticipantResult, Report, Rubric, Step, criteria_of,
                               evaluate_card, verdict)
from domain.calls import CallPack, load_rubric112
from domain.desk import CallDesk
from domain.lesson import (RECONNECT_GRACE_S, Actor, InvalidRole, Lesson, LessonError, Mode, Role, State)
from domain.scenario import CardViews, Pack, Router

Out = Tuple[str, dict]

CALLER_BRIGADE = "Старший бригады"
MONITOR_PERIOD_S = 1.0


class NotFound(LessonError):
    pass


@dataclass
class Track:
    """Одна карточка у одного участника: его статусы и доставленные ему доклады."""
    number: str
    key: str
    added_at: float
    opened_at: Optional[float] = None
    steps: List[Step] = field(default_factory=list)
    order_no: str = ""
    reports: List[Report] = field(default_factory=list)
    brigade_started: bool = False
    closed: bool = False

    def current(self) -> Optional[str]:
        return self.steps[-1].status if self.steps else None


@dataclass
class CardRuntime:
    key: str
    number: str
    spec: dict
    view: dict
    created_at: float
    bot_statuses: Dict[str, List[dict]] = field(default_factory=dict)


class LessonRuntime:
    def __init__(self, lesson: Lesson, pack, time_scale: float, seed: int, config: Optional[dict] = None):
        self.lesson = lesson
        self.pack = pack                  # Pack (ДДС) или CallPack (оператор 112)
        self.time_scale = time_scale
        self.config: dict = dict(config or {})
        self.desk: Optional[CallDesk] = None  # только в режиме «карточки»
        self.rng = random.Random("%s:%s" % (seed, lesson.id))
        self.names: Dict[str, str] = {}
        self.cards: Dict[str, CardRuntime] = {}
        self.numbers: Dict[str, str] = {}
        self.tracks: Dict[Tuple[str, str], Track] = {}
        self.queue: list = []
        self.last_seq: Dict[str, int] = {}
        self.offline_since: Dict[str, float] = {}
        self.offline_findings: Dict[str, List[Finding]] = {}
        self.results: Optional[dict] = None
        self.log: List[dict] = []  # журнал событий занятия — основа записи и отчёта
        self.monitor_at = float("-inf")

    @property
    def cards_mode(self) -> bool:
        return getattr(self.pack, "mode", "card_actions") == "cards"

    def participants(self) -> List[str]:
        return [p for p, s in self.lesson.seats.items() if s.role]

    def tracks_of(self, pid: str) -> List[Track]:
        return [t for (p, _), t in self.tracks.items() if p == pid]


class Engine:
    def __init__(self, router: Router, views: CardViews, fsm: ResponseFsm, rubric: Rubric,
                 packs: Dict[str, Pack], clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, seed: int = 112,
                 on_log: Optional[Callable[["LessonRuntime", dict], None]] = None,
                 rubric112: Optional[dict] = None, textcheck=None, speech=None):
        self.router, self.views, self.fsm, self.rubric = router, views, fsm, rubric
        self.rubric112 = rubric112 or load_rubric112()
        self.textcheck = textcheck    # domain.textcheck.TextChecker (узел 3); None — без проверки текста
        self.speech = speech          # speech.catalog.SpeechCatalog (узел 5): ссылки на готовый звук реплик
        self.packs = packs
        self.clock, self._wall = clock, wall
        self._mono0, self._wall0 = clock(), wall()
        self.seed = seed
        self.lessons: Dict[str, LessonRuntime] = {}
        self._ids = itertools.count(1)
        self.on_log = on_log  # прикладной слой пишет журнал событий в базу
        self.monitor_period_s = MONITOR_PERIOD_S  # администратор настраивает (узел 7)
        self._seq = itertools.count()

    # --- вспомогательное -----------------------------------------------------
    def wall(self, mono: float) -> float:
        """Отображаемое время для серверного момента mono."""
        return self._wall0 + (mono - self._mono0)

    def get(self, lesson_id: str) -> LessonRuntime:
        if lesson_id not in self.lessons:
            raise NotFound("нет занятия %s" % lesson_id)
        return self.lessons[lesson_id]

    def _schedule(self, rt: LessonRuntime, due: float, kind: str, data: tuple) -> None:
        heapq.heappush(rt.queue, (due, next(self._seq), kind, data))

    def _log(self, rt: LessonRuntime, now: float, kind: str, **data) -> dict:
        entry = {"at": self.wall(now), "kind": kind, **data}
        rt.log.append(entry)
        if self.on_log:
            self.on_log(rt, entry)
        return entry

    def _teacher(self, rt: LessonRuntime) -> str:
        return "teacher:" + rt.lesson.owner_id

    def _rubric(self, rt: LessonRuntime) -> Rubric:
        """Рубрика занятия: норматив первичного статуса задаёт преподаватель (ТЗ: по умолчанию 30 с)."""
        norm = rt.config.get("primary_norm_s")
        return dataclasses.replace(self.rubric, primary_norm_s=float(norm)) if norm else self.rubric

    def _checker(self, rt: LessonRuntime):
        """Проверка текста — если преподаватель не отключил её в критериях занятия."""
        return self.textcheck if rt.config.get("text_check", True) else None

    def _timing(self, rt: LessonRuntime) -> dict:
        lesson = rt.lesson
        return {"started_at": self.wall(lesson.started_at) if lesson.started_at is not None else None,
                "finished_at": self.wall(lesson.finished_at) if lesson.finished_at is not None else None,
                "time_scale": rt.time_scale}

    # --- лобби (команды преподавателя и участников) ----------------------------
    def build_pack(self, raw: dict):
        """Пакет из JSON (сгенерированный, из базы) — с той же проверкой, что пакеты из data/scenarios."""
        if raw.get("mode") == "cards":
            pack = CallPack(raw)
            pack.validate(self.router.classifier)
        else:
            pack = Pack(raw)
            pack.validate(self.router.classifier, self.router.territory, self.fsm, self.router)
        return pack

    def create_lesson(self, actor: Actor, pack_id: str, time_scale: float = 1.0,
                      lesson_id: Optional[str] = None, config: Optional[dict] = None, pack=None) -> LessonRuntime:
        """lesson_id задаёт прикладной слой (по id строки в базе), чтобы номера не повторялись после перезапуска.
        pack — собранный для этого занятия пакет (например, с карточками обучающихся) вместо пакета из списка."""
        if pack is None and pack_id not in self.packs:
            raise NotFound("нет пакета сценариев %s" % pack_id)
        lesson_id = lesson_id or "L%d" % next(self._ids)
        if lesson_id in self.lessons:
            raise LessonError("занятие %s уже есть" % lesson_id)
        pack = pack or self.packs[pack_id]
        lesson = Lesson(lesson_id, actor.user_id, self.clock)
        lesson.configure(actor, mode=Mode.CARDS if getattr(pack, "mode", "") == "cards" else Mode.CARD_ACTIONS)
        lesson.open(actor)
        rt = LessonRuntime(lesson, pack, time_scale, self.seed, config)
        self.lessons[lesson.id] = rt
        return rt

    def join(self, lesson_id: str, pid: str, name: str) -> List[Out]:
        rt = self.get(lesson_id)
        rt.lesson.join(pid)
        rt.names[pid] = name
        return [(self._teacher(rt), self.monitor(lesson_id))]

    def assign_role(self, actor: Actor, lesson_id: str, pid: str, service: str) -> List[Out]:
        rt = self.get(lesson_id)
        if rt.cards_mode:  # в режиме «карточки» все обучающиеся — операторы Службы 112
            rt.lesson.assign_role(actor, pid, Role.operator_112())
            return [(pid, self.snapshot(lesson_id, pid)), (self._teacher(rt), self.monitor(lesson_id))]
        if service != rt.pack.role:
            # Эталон пакета написан для одной роли — по другой службе оценивать не с чем.
            raise InvalidRole("в сценарии «%s» роль — «%s»" % (rt.pack.title, rt.pack.role_title))
        rt.lesson.assign_role(actor, pid, Role.dds(service))
        return [(pid, self.snapshot(lesson_id, pid)), (self._teacher(rt), self.monitor(lesson_id))]

    def start(self, actor: Actor, lesson_id: str) -> List[Out]:
        rt = self.get(lesson_id)
        rt.lesson.start(actor)
        now = self.clock()
        if rt.cards_mode:
            cfg = rt.config
            rt.desk = CallDesk(rt.pack, self.router.classifier, self.router, self.rubric112, int(rt.lesson.id[1:]),
                               "%s:%s" % (self.seed, rt.lesson.id), self.wall, count=int(cfg.get("count") or 8),
                               feedback=cfg.get("feedback") or "immediate", norm_s=cfg.get("norm_s"),
                               groups=cfg.get("groups"), checker=self._checker(rt), speech=self.speech,
                               answer_norm_s=cfg.get("answer_norm_s"))
            rt.desk.start(rt.participants(), now, lambda due, kind, data: self._schedule(rt, due, kind, data))
            self._log(rt, now, "start", calls=rt.desk.keys)
            outs = [(pid, self.snapshot(lesson_id, pid)) for pid in rt.lesson.seats]
            return outs + [(self._teacher(rt), self.monitor(lesson_id))]
        for i, spec in enumerate(rt.pack.cards):
            rt.numbers[spec["key"]] = str(36900000 + int(rt.lesson.id[1:]) * 1000 + 101 + i)
            self._schedule(rt, now + spec["at"] * rt.time_scale, "card", (spec["key"],))
        self._log(rt, now, "start")
        outs = [(pid, self.snapshot(lesson_id, pid)) for pid in rt.lesson.seats]
        return outs + [(self._teacher(rt), self.monitor(lesson_id))]

    def finish(self, actor: Actor, lesson_id: str) -> List[Out]:
        rt = self.get(lesson_id)
        rt.lesson.finish(actor)
        now = self.clock()
        for pid in list(rt.offline_since):
            self._close_offline(rt, pid, now)
        if rt.cards_mode:
            results = [dict(rt.desk.results(pid, now), name=rt.names.get(pid, pid))
                       for pid in rt.participants()] if rt.desk else []
            categories = results[0]["categories"] if results else []
            return self._finished(rt, now, results, categories)
        categories = [{"category": c, "title": v["title"], "weight": v["weight"]}
                      for c, v in sorted(self.rubric.categories.items())]
        results = []
        rubric, checker = self._rubric(rt), self._checker(rt)
        for pid in rt.participants():
            findings = list(rt.offline_findings.get(pid, []))
            for track in sorted(rt.tracks_of(pid), key=lambda t: t.added_at):
                spec = rt.pack.by_key[track.key]
                findings += evaluate_card(spec["expect"], track.added_at, track.steps, track.reports,
                                          rt.numbers, rubric, card=track.number, checker=checker)
            res = ParticipantResult(pid, rt.lesson.seats[pid].role.service, findings, rubric.score(findings))
            results.append(dict(res.to_dict(self.rubric), name=rt.names.get(pid, pid),
                                cards=len(rt.tracks_of(pid)), categories=categories))
        return self._finished(rt, now, results, categories)

    def _finished(self, rt: LessonRuntime, now: float, results: List[dict], categories: List[dict]) -> List[Out]:
        criteria = criteria_of(rt.config)
        for r in results:  # зачёт по критериям занятия (узел 6)
            r.update(criteria=criteria, **verdict(r["score"], len(r["findings"]), criteria))
        rt.results = {"lesson": rt.lesson.id, "pack": rt.pack.title,
                      "mode": "cards" if rt.cards_mode else "card_actions",
                      "categories": categories, "participants": results}
        rt.lesson.mark_reported()
        self._log(rt, now, "finish")
        outs = [(r["participant"], {"t": "result", "result": r}) for r in results]
        return outs + [(self._teacher(rt), {"t": "results", "results": rt.results}),
                       (self._teacher(rt), self.monitor(rt.lesson.id))]

    # --- связь участника (§18.6) ------------------------------------------------
    def connect(self, lesson_id: str, pid: str) -> List[Out]:
        rt = self.get(lesson_id)
        if pid not in rt.lesson.seats:
            return []
        now = self.clock()
        if pid in rt.offline_since:
            self._close_offline(rt, pid, now)
        rt.lesson.reconnect(pid)
        return [(pid, self.snapshot(lesson_id, pid)), (self._teacher(rt), self.monitor(lesson_id))]

    def disconnect(self, lesson_id: str, pid: str) -> List[Out]:
        rt = self.get(lesson_id)
        if pid not in rt.lesson.seats:
            return []
        rt.lesson.disconnect(pid)
        if rt.lesson.state is State.RUNNING:
            rt.offline_since.setdefault(pid, self.clock())
        return [(self._teacher(rt), self.monitor(lesson_id))]

    def _close_offline(self, rt: LessonRuntime, pid: str, now: float) -> None:
        gone = now - rt.offline_since.pop(pid)
        if gone > RECONNECT_GRACE_S and not rt.cards_mode:  # в режиме 112 связь видна по пропущенным вызовам
            rt.offline_findings.setdefault(pid, []).append(
                Finding(7, "нет связи с рабочим местом %.0f с" % gone))

    # --- ход занятия --------------------------------------------------------------
    def tick(self) -> List[Out]:
        now = self.clock()
        outs: List[Out] = []
        for rt in self.lessons.values():
            while rt.queue and rt.queue[0][0] <= now:
                due, _, kind, data = heapq.heappop(rt.queue)
                if rt.lesson.state is not State.RUNNING:
                    continue
                outs += getattr(self, "_on_" + kind)(rt, due, *data)
            if rt.lesson.state is State.RUNNING and now - rt.monitor_at >= self.monitor_period_s:
                rt.monitor_at = now  # просрочки растут со временем — монитор обновляется и без событий
                outs.append((self._teacher(rt), self.monitor(rt.lesson.id)))
        return outs

    def _on_ring(self, rt: LessonRuntime, now: float, pid: str) -> List[Out]:
        outs = rt.desk.ring(pid, now)
        if outs:
            self._log(rt, now, "ring", participant=pid, card=outs[0][1]["call"]["number"])
        return outs

    def _on_card(self, rt: LessonRuntime, now: float, key: str) -> List[Out]:
        spec = rt.pack.by_key[key]
        number = rt.numbers[key]
        place = rt.pack.place_for(spec)
        plaques = self.router.plaques(spec["code"], spec.get("flags", []), place, spec.get("manual_services", []))
        view = self.views.build(spec, number, self.wall(now), place, plaques)
        card = CardRuntime(key, number, spec, view, now)
        rt.cards[number] = card
        for p in plaques:
            if p.name != rt.pack.role:  # остальные службы ведут боты — для реалистичной полосы служб
                card.bot_statuses[p.name] = [{"status": "Добавлена", "at": self.wall(now), "by": "оп. 0"}]
                self._schedule(rt, now + rt.rng.uniform(5, 40) * rt.time_scale, "bot", (number, p.name, "Получена службой"))
                self._schedule(rt, now + rt.rng.uniform(45, 120) * rt.time_scale, "bot", (number, p.name, "Принята"))
        recipients = rt.lesson.recipients([p.name for p in plaques])
        outs = []
        for pid in sorted(recipients):
            rt.tracks[(pid, number)] = Track(number, key, now)
            outs.append((pid, {"t": "card", "card": self._card_for(rt, pid, card)}))
        self._log(rt, now, "card", card=number, key=key, recipients=sorted(recipients))
        return outs

    def _on_bot(self, rt: LessonRuntime, now: float, number: str, service: str, status: str) -> List[Out]:
        entry = {"status": status, "at": self.wall(now), "by": "оп. 0"}
        rt.cards[number].bot_statuses[service].append(entry)
        return [(pid, {"t": "card_status", "card": number, "service": service, "entry": entry})
                for (pid, n) in rt.tracks if n == number]

    def _on_report(self, rt: LessonRuntime, now: float, pid: str, number: str, index: int) -> List[Out]:
        track = rt.tracks[(pid, number)]
        if track.closed:
            return []  # карточка закрыта — бригада больше не докладывает
        rep = rt.cards[number].spec["brigade"][index]
        track.reports.append(Report(now, rep["status"], rep["text"]))
        call = {"t": "call", "card": number, "from": CALLER_BRIGADE, "text": rep["text"], "at": self.wall(now)}
        audio = self.speech.brigade_audio(rep["text"]) if self.speech else None
        if audio:  # доклад звучит голосом, если уже озвучен; текст на экране — всегда
            call["audio"] = audio
        self._log(rt, now, "call", participant=pid, card=number, text=rep["text"])
        return [(pid, call)]

    # --- действия участника ---------------------------------------------------------
    def handle(self, lesson_id: str, pid: str, msg: dict) -> List[Out]:
        rt = self.get(lesson_id)
        kind = msg.get("t")
        if rt.cards_mode:
            if rt.lesson.state is not State.RUNNING or rt.desk is None:
                return [(pid, {"t": "error", "reason": "занятие не идёт"})]
            now = self.clock()
            outs = rt.desk.handle(pid, msg, now, lambda due, k, data: self._schedule(rt, due, k, data),
                                  lambda k, **data: self._log(rt, now, k, **data))
            if kind == "save_card":
                outs.append((self._teacher(rt), self.monitor(lesson_id)))
            return outs
        if kind == "open_card":
            return self._open_card(rt, pid, str(msg.get("card")))
        if kind == "set_status":
            return self._set_status(rt, pid, msg)
        return [(pid, {"t": "error", "reason": "неизвестное сообщение"})]

    def _open_card(self, rt: LessonRuntime, pid: str, number: str) -> List[Out]:
        track = rt.tracks.get((pid, number))
        if track is None or track.opened_at is not None:
            return []
        now = self.clock()
        track.opened_at = now  # памятка: «Получена службой» — автоматически при открытии карточки на АРМ
        entry = {"status": "Получена службой", "at": self.wall(now), "by": "оп. 0"}
        return [(pid, {"t": "card_status", "card": number, "service": rt.pack.role, "entry": entry})]

    def _set_status(self, rt: LessonRuntime, pid: str, msg: dict) -> List[Out]:
        seq = int(msg.get("seq", 0))
        if seq and seq <= rt.last_seq.get(pid, 0):
            return [(pid, {"t": "status_ack", "seq": seq, "duplicate": True})]  # повтор после переподключения

        def reject(reason):
            return [(pid, {"t": "status_rejected", "seq": seq, "reason": reason})]

        if rt.lesson.state is not State.RUNNING:
            return reject("занятие не идёт")
        number = str(msg.get("card"))
        track = rt.tracks.get((pid, number))
        if track is None:
            return reject("нет такой карточки")
        status = msg.get("status", "")
        allowed = self.fsm.allowed_next(track.current())
        if status not in allowed:
            return reject("после «%s» доступно: %s" % (track.current() or "—", ", ".join(allowed) or "ничего"))

        now = self.clock()
        comment = str(msg.get("comment", "")).strip()
        track.steps.append(Step(status, now, comment))
        track.order_no = str(msg.get("order_no", track.order_no)).strip()
        if seq:
            rt.last_seq[pid] = seq
        outs: List[Out] = []
        spec = rt.cards[number].spec
        if status == "Принята" and not track.brigade_started and spec.get("brigade"):
            track.brigade_started = True
            for i, rep in enumerate(spec["brigade"]):
                self._schedule(rt, now + rep["after"] * rt.time_scale, "report", (pid, number, i))
        if status in TERMINAL:
            track.closed = True
        entry = {"status": status, "at": self.wall(now), "by": rt.names.get(pid, pid), "comment": comment,
                 "order_no": track.order_no}
        self._log(rt, now, "status", participant=pid, card=number, status=status, comment=comment)
        outs.append((pid, {"t": "status_ack", "seq": seq}))
        outs.append((pid, {"t": "card_status", "card": number, "service": rt.pack.role, "entry": entry,
                           "allowed": self.fsm.allowed_next(track.current()), "order_no": track.order_no}))
        outs.append((self._teacher(rt), self.monitor(rt.lesson.id)))
        return outs

    # --- представления ------------------------------------------------------------------
    def _own_statuses(self, rt: LessonRuntime, track: Track) -> List[dict]:
        own = [{"status": "Добавлена", "at": self.wall(track.added_at), "by": "оп. 0"}]
        if track.opened_at is not None:
            own.append({"status": "Получена службой", "at": self.wall(track.opened_at), "by": "оп. 0"})
        own += [{"status": s.status, "at": self.wall(s.at), "comment": s.comment} for s in track.steps]
        return sorted(own, key=lambda e: e["at"])

    def _card_for(self, rt: LessonRuntime, pid: str, card: CardRuntime) -> dict:
        track = rt.tracks[(pid, card.number)]
        statuses = {name: list(entries) for name, entries in card.bot_statuses.items()}
        statuses[rt.pack.role] = self._own_statuses(rt, track)
        return dict(card.view, statuses=statuses, own_service=rt.pack.role,
                    allowed=self.fsm.allowed_next(track.current()), order_no=track.order_no,
                    calls=[{"at": self.wall(r.at), "text": r.text, "from": CALLER_BRIGADE} for r in track.reports])

    def snapshot(self, lesson_id: str, pid: str) -> dict:
        rt = self.get(lesson_id)
        seat = rt.lesson.seats.get(pid)
        if rt.cards_mode:
            return {
                "t": "state",
                "server_time": self.wall(self.clock()),
                "lesson": dict(self._timing(rt), id=lesson_id, state=rt.lesson.state.value, title=rt.pack.title,
                               mode="cards", role="Оператор 112" if seat and seat.role else None,
                               role_title=rt.pack.role_title, reference=rt.pack.reference,
                               norm_s=float(rt.config.get("norm_s") or rt.pack.norm_s),
                               solo=bool(rt.config.get("solo"))),
                "desk": rt.desk.snapshot(pid) if rt.desk and pid in rt.desk.done else None,
                "last_seq": rt.desk.last_seq.get(pid, 0) if rt.desk else 0,
                "result": next((r for r in (rt.results or {}).get("participants", [])
                                if r["participant"] == pid), None),
            }
        cards = [self._card_for(rt, pid, rt.cards[n]) for (p, n) in rt.tracks if p == pid]
        return {
            "t": "state",
            "server_time": self.wall(self.clock()),
            "lesson": dict(self._timing(rt), id=lesson_id, state=rt.lesson.state.value, title=rt.pack.title,
                           role=seat.role.service if seat and seat.role else None, solo=bool(rt.config.get("solo")),
                           role_title=rt.pack.role_title, reference=rt.pack.reference,
                           primary_norm_s=self._rubric(rt).primary_norm_s),
            "cards": sorted(cards, key=lambda c: c["saved_at"], reverse=True),
            # клиент продолжает нумерацию своих команд отсюда — иначе после перезагрузки
            # страницы новые команды совпали бы по номеру со старыми и были бы отброшены как повтор
            "last_seq": rt.last_seq.get(pid, 0),
            "result": next((r for r in (rt.results or {}).get("participants", []) if r["participant"] == pid), None),
        }

    def monitor(self, lesson_id: str) -> dict:
        """Экран преподавателя (§18.10): участники, связь, нагрузка, просрочки — вживую."""
        rt = self.get(lesson_id)
        now = self.clock()
        rows = []
        if rt.cards_mode:
            for pid, seat in rt.lesson.seats.items():
                row = {"participant": pid, "name": rt.names.get(pid, pid),
                       "role": "Оператор 112" if seat.role else None,
                       "connection": rt.lesson.connection(pid).value}
                if rt.desk and pid in rt.desk.done:
                    row.update(rt.desk.monitor_row(pid, now))
                rows.append(row)
            total = len(rt.desk.keys) if rt.desk else int(rt.config.get("count") or 8)
            return {"t": "monitor", "server_time": self.wall(now),
                    "lesson": dict(self._timing(rt), id=lesson_id, state=rt.lesson.state.value, mode="cards",
                                   pack=rt.pack.title, role=None, role_title=rt.pack.role_title,
                                   cards_total=total, norm_s=float(rt.config.get("norm_s") or rt.pack.norm_s),
                                   cards_sent=sum(r.get("calls_done", 0) for r in rows)),
                    "participants": rows}
        for pid, seat in rt.lesson.seats.items():
            tracks = rt.tracks_of(pid)
            no_primary = [t for t in tracks if not t.steps]
            overdue = [t for t in no_primary if now - t.added_at > self._rubric(rt).primary_norm_s]
            late_progress = sum(1 for t in tracks for r in t.reports
                                if now - r.at > self.rubric.progress_window_s
                                and not any(s.status == r.status for s in t.steps))
            rows.append({"participant": pid, "name": rt.names.get(pid, pid),
                         "role": seat.role.service if seat.role else None,
                         "connection": rt.lesson.connection(pid).value,
                         "cards": len(tracks), "waiting": len(no_primary),
                         "overdue": len(overdue), "late_progress": late_progress})
        return {"t": "monitor", "server_time": self.wall(now),
                "lesson": dict(self._timing(rt), id=lesson_id, state=rt.lesson.state.value, mode="card_actions",
                               pack=rt.pack.title, role=rt.pack.role, role_title=rt.pack.role_title,
                               cards_total=len(rt.pack.cards), cards_sent=len(rt.cards)),
                "participants": rows}
