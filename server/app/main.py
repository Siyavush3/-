"""HTTP и WebSocket тренажёра (ARCHITECTURE.md §18.7, §19).

Все обработчики асинхронные: движок однопоточный и вызывается только из цикла событий,
вместе с тикером. Синхронные обработчики FastAPI увёл бы в пул потоков, и движок
работал бы параллельно с тикером. Обращения к базе короткие и синхронные (store.db).

Права: каждая ручка объявляет роли (require). Пользователь с временным паролем до смены
пароля может только посмотреть себя, сменить пароль и выйти.

Запуск:  python server/run.py
"""
import asyncio
import logging
import types
import hashlib
import json
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional, Set

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import arm112
from app.audit import Audit
from app.auth import ROLE_TITLES, Accounts, AuthError, User
from arm112.ekp_compiler import build as build_ekp_table
from arm112.paths import REPO_ROOT, data_dir
from arm112.reference.fsm import ResponseFsm
from arm112.reference.routing import Classifier
from arm112.reference.territory import Territory
from domain import generator as gen
from domain.calls import CallPack, detect_intent, questionnaire
from domain.textcheck import TextChecker, available as textcheck_available, domain_words
from domain.engine import Engine, NotFound
from domain.evaluation import Rubric, criteria_of, verdict
from domain.lesson import LessonError, PermissionDenied, State
from domain.llm import LocalLLM
from domain.scenario import CardViews, Pack, Router
from speech.asr import MODEL as ASR_MODEL, RATE as ASR_RATE, Asr, AsrUnavailable
from speech.catalog import SpeechCatalog, pack_phrases
from speech.queue import TtsQueue
from store.db import Database
from store.repos import (APPROVED, ARCHIVED, DRAFT, Events, Feedback, Groups, Lessons, Materials, Results,
                         Scenarios, StudentCards, Tasks, pid_to_id)
from app import admin_ops, teaching
from ops import backup as bk
from ops.backup import Backups
from ops.logs import SystemLog
from ops.monitor import SAMPLE_S, Monitor
from ops.settings import Settings
from store.schema import migrate

TICK_S = 0.2
HELLO_TIMEOUT_S = 10.0
VOICE_CHUNK_MAX = 64 * 1024   # байт звука в одном сообщении микрофона (2 с при 16 кГц)
VOICE_AHEAD_S = 10.0          # звук не может приходить быстрее речи: запас на досылку после задержки сети
STEPUP_S = 600.0      # подтверждение пароля администратором действует 10 минут
LIVE_STATES = (State.RECRUITING.value, State.RUNNING.value)


def load_router(classifier: Classifier, territory: Territory, ui: dict) -> Router:
    """Маршрутизация через C-ядро, если оно собрано; иначе — через эталон (результат тот же).

    Таблицу собираем из тех же JSON при старте: так она не может отстать от данных.
    Имя файла — по содержимому, поэтому перезапуски не плодят файлы и не пишут в таблицу,
    которую держит отображённой другой процесс.
    """
    try:
        from arm112.ekp import Ekp
        with open(data_dir() / "classifier" / "classifier.json", encoding="utf-8") as f:
            blob = build_ekp_table(json.load(f), ui)
        path = Path(tempfile.gettempdir()) / ("arm112-%s.ekp" % hashlib.sha256(blob).hexdigest()[:16])
        if not path.is_file():
            fd, tmp = tempfile.mkstemp(prefix="arm112-", suffix=".tmp", dir=str(path.parent))
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            os.replace(tmp, str(path))
        return Router(classifier, territory, ui, ekp=Ekp(path))
    except OSError:  # библиотека не собрана
        return Router(classifier, territory, ui)


def load_engine() -> Engine:
    classifier = Classifier.load()
    territory = Territory.load()
    with open(data_dir() / "classifier" / "ui_services.json", encoding="utf-8") as f:
        ui = json.load(f)
    router = load_router(classifier, territory, ui)
    fsm = ResponseFsm.load()
    packs: Dict[str, object] = {}
    for path in sorted((data_dir() / "scenarios").glob("*.json")):
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if raw.get("mode") == "cards":            # вызовы для оператора 112
            pack = CallPack(raw, str(path))
            pack.validate(classifier)
        elif "cards" in raw:                        # карточки для ДДС
            pack = Pack(raw, str(path))
            pack.validate(classifier, territory, fsm, router)
        else:
            continue  # корпус билетов и т.п. — не пакет занятия
        packs[pack.id] = pack
    textcheck = None
    if textcheck_available():  # проверка текста (узел 3): без pymorphy3 оценка работает, но без орфографии
        with open(data_dir() / "services" / "services.json", encoding="utf-8") as f:
            services = [x["short"] for x in json.load(f)["services"]]
        words = domain_words(classifier, packs.values(), services) | set(gen.vocabulary(gen.Gazetteer.load()))
        textcheck = TextChecker(words)
    return Engine(router, CardViews.load(classifier), fsm, Rubric.load(), packs, textcheck=textcheck)


class Connections:
    """Открытые WebSocket по адресатам движка: участник или «teacher:<id>»."""

    def __init__(self):
        self._by_target: Dict[str, Set[WebSocket]] = {}

    def add(self, target: str, ws: WebSocket) -> None:
        self._by_target.setdefault(target, set()).add(ws)

    def remove(self, target: str, ws: WebSocket) -> None:
        self._by_target.get(target, set()).discard(ws)

    def has(self, target: str) -> bool:
        return bool(self._by_target.get(target))

    def count(self) -> int:
        return sum(len(v) for v in self._by_target.values())

    async def deliver(self, outs) -> None:
        for target, msg in outs:
            for ws in list(self._by_target.get(target, ())):
                try:
                    await ws.send_json(msg)
                except Exception:  # сокет закрылся между событиями — уберёт обработчик разрыва
                    self.remove(target, ws)


# ---------- тела запросов ----------
class LoginIn(BaseModel):
    login: str
    password: str


class PasswordIn(BaseModel):
    old: str
    new: str


class ConfirmIn(BaseModel):
    password: str


class LessonIn(BaseModel):
    pack_id: str
    time_scale: float = 1.0
    count: int = 8                       # «карточки»: сколько вызовов у каждого обучающегося
    feedback: str = "immediate"          # «карточки»: разбор сразу после карточки или только в итоге
    norm_s: Optional[float] = None       # «карточки»: норматив заполнения карточки, с
    groups: Optional[List[str]] = None   # категории событий (группы классификатора); пусто — все
    source: str = "pack"                 # ДДС: карточки сценария (pack), обучающихся (student) или вперемешку (mixed)
    # критерии успешности и нормативы (узел 6); не задано — по умолчанию
    pass_score: Optional[int] = None     # порог зачёта, баллов из 100 (по умолчанию 70)
    max_errors: Optional[int] = None     # допустимое число замечаний (по умолчанию не ограничено)
    text_check: Optional[bool] = None    # учитывать орфографию и оформление (по умолчанию да)
    primary_norm_s: Optional[float] = None  # ДДС: норматив первичного статуса, с (ТЗ: по умолчанию 30)
    answer_norm_s: Optional[float] = None   # 112: норматив принятия вызова, с (по умолчанию 15)
    group_id: Optional[int] = None       # занятие для группы: войти могут только её участники


class GenerateIn(BaseModel):
    mode: str = "card_actions"           # card_actions — карточки ДДС, cards — вызовы оператору 112
    district: Optional[str] = None
    difficulty: int = 1
    count: int = 10
    groups: Optional[List[str]] = None
    seed: Optional[int] = None


class ReviseIn(BaseModel):
    comment: str


class ApproveIn(BaseModel):
    keys: Optional[List[str]] = None     # частичное утверждение: только отмеченные карточки
    title: Optional[str] = None


class RoleIn(BaseModel):
    participant: str
    service: str


class TextIn(BaseModel):
    text: str


class UserIn(BaseModel):
    login: str
    name: str
    role: str


class UserPatch(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    blocked: Optional[bool] = None


def lesson_db_id(lesson_id: str) -> int:
    if not (lesson_id[:1] == "L" and lesson_id[1:].isdigit()):
        raise HTTPException(404, "нет занятия %s" % lesson_id)
    return int(lesson_id[1:])


def create_app(engine: Optional[Engine] = None, db: Optional[Database] = None, tick_s: float = TICK_S,
               clock=time.time, tts_dir=None, voices_dir=None, tts_autostart: Optional[bool] = None,
               asr: Optional[Asr] = None) -> FastAPI:
    conns = Connections()
    stepups: Dict[str, float] = {}
    # озвучка (узел 5): готовые WAV лежат в var/tts и раздаются как статика; синтез — отдельным процессом
    tts_path = Path(tts_dir or os.environ.get("A112_TTS_DIR") or REPO_ROOT / "var" / "tts")
    tts_path.mkdir(parents=True, exist_ok=True)
    voices_path = Path(voices_dir or os.environ.get("A112_VOICES") or data_dir() / "voices")
    var_path = Path(os.environ.get("A112_VAR") or REPO_ROOT / "var")
    materials_path = Path(os.environ.get("A112_MATERIALS_DIR") or var_path / "materials")
    if tts_autostart is None:
        tts_autostart = os.environ.get("A112_TTS", "on") != "off"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st = app.state
        st.db = db or Database()
        migrate(st.db)
        st.accounts = Accounts(st.db, clock)
        st.audit = Audit(st.db, clock)
        st.settings = Settings(st.db)
        st.syslog = SystemLog(os.environ.get("A112_LOG_DIR") or var_path / "logs", st.settings.get("logging.max_mb"),
                              st.settings.get("logging.level"))
        st.monitor = Monitor(var_path, clock)
        st.backups = Backups(st.db, os.environ.get("A112_BACKUP_DIR") or var_path / "backups", materials_path,
                             arm112.__version__, clock)
        st.lessons = Lessons(st.db, clock)
        st.events = Events(st.db)
        st.results = Results(st.db, clock)
        st.scenarios = Scenarios(st.db, clock)
        st.groups = Groups(st.db, clock)
        st.tasks = Tasks(st.db, clock)
        st.materials = Materials(st.db, clock)
        st.feedback = Feedback(st.db, clock)
        st.student_cards = StudentCards(st.db, clock)
        st.gazetteer = gen.Gazetteer.load()
        st.llm = LocalLLM()
        st.speech = SpeechCatalog(tts_path)
        st.speech.ensure_ring()
        st.tts = TtsQueue(st.speech, voices_path, autostart=tts_autostart)
        # распознавание речи оператора (узел 9): модель Vosk в data/asr, свой пул потоков
        st.asr = asr or Asr(os.environ.get("A112_ASR_MODEL") or data_dir() / "asr" / ASR_MODEL,
                            data_dir() / "asr" / "vocabulary.txt", threads=int(os.environ.get("A112_ASR_THREADS") or 2))
        st.asr.enabled = bool(st.settings.get("voice.asr"))
        st.started = clock()
        interrupted = st.lessons.mark_interrupted(LIVE_STATES)
        if interrupted:
            st.audit.log("system.lessons_interrupted", details={"count": interrupted})
        st.engine = engine or load_engine()
        st.engine.on_log = persist_event
        for row in st.scenarios.list([APPROVED]):  # утверждённые сценарии доступны и после перезапуска
            try:
                st.engine.packs[row["slug"]] = st.engine.build_pack(row["body"])
            except (ValueError, KeyError) as exc:
                st.audit.log("scenario.invalid", obj=row["slug"], details={"error": str(exc)})
        st.engine.speech = st.speech

        def apply_settings(s, changed):  # настройки администратора — сразу в работающие объекты (узел 7)
            acc = st.accounts
            acc.password_min, acc.max_failed = s.get("security.password_min"), s.get("security.max_failed")
            acc.lock_s, acc.session_ttl_s = s.get("security.lock_minutes") * 60.0, s.get("security.session_hours") * 3600.0
            st.syslog.configure(s.get("logging.max_mb") if "logging.max_mb" in changed else None,
                                s.get("logging.level") if "logging.level" in changed else None)
            st.syslog.requests = s.get("logging.requests")
            st.engine.monitor_period_s = s.get("performance.monitor_s")
            st.tts.profile = s.get("voice.profile")
            if "voice.enabled" in changed and tts_autostart:
                st.tts.resume() if s.get("voice.enabled") else st.tts.pause()
            st.asr.enabled = bool(s.get("voice.asr"))

        st.settings.on_change(apply_settings)
        for pack in st.engine.packs.values():  # озвучить то, что ещё не озвучено, — фоном, одним ядром
            st.tts.enqueue(pack_phrases(pack.raw))
        st.syslog.log(logging.INFO, "сервер запущен", kind="start", version=arm112.__version__,
                      database=st.db.dialect, packs=len(st.engine.packs))
        scheduled = [b for b in st.backups.list() if b.get("reason") == "по расписанию"]
        st.last_scheduled_backup = scheduled[0]["created_at"] if scheduled else None
        st.accounts.purge_sessions()

        async def ticker():
            while True:
                await asyncio.sleep(tick_s)
                await conns.deliver(st.engine.tick())

        async def sampler():  # нагрузка для мониторинга администратора: история за час
            while True:
                await asyncio.sleep(SAMPLE_S)
                try:
                    running = sum(rt.lesson.state is State.RUNNING for rt in st.engine.lessons.values())
                    st.monitor.sample({"lessons": running, "connections": conns.count(), "tts_pending": len(st.tts.pending)})
                except Exception as exc:  # мониторинг не должен ронять сервер
                    st.syslog.failure("monitor", "замер нагрузки не удался: %s" % exc)

        async def backup_scheduler():  # ТЗ: резервное копирование не реже одного раза в сутки
            while True:
                await asyncio.sleep(30)
                s = st.settings
                if s.get("backup.enabled") and bk.due(clock(), s.get("backup.time"), st.last_scheduled_backup):
                    st.last_scheduled_backup = clock()
                    try:
                        item = await ops_ctx.make_backup("по расписанию", "system")
                        st.audit.log("backup.auto", obj=item["name"], details={"size": item["size"]})
                    except bk.BackupError:
                        pass  # сбой уже в журнале и в состоянии компонентов

        tasks = [asyncio.create_task(ticker()), asyncio.create_task(sampler()), asyncio.create_task(backup_scheduler())]
        if st.asr.enabled and os.environ.get("A112_ASR", "on") != "off":
            tasks.append(asyncio.create_task(st.asr.ready()))  # модель — заранее, чтобы первый микрофон не ждал
        yield
        for task in tasks:
            task.cancel()
        st.asr.close()
        st.syslog.log(logging.INFO, "сервер остановлен", kind="stop")
        st.syslog.close()

    app = FastAPI(title="Тренажёр оператора ДДС (АРМ-112)", version=arm112.__version__, lifespan=lifespan)

    def eng() -> Engine:
        return app.state.engine

    def persist_event(rt, entry: dict) -> None:
        data = {k: v for k, v in entry.items() if k not in ("at", "kind")}
        app.state.events.add(lesson_db_id(rt.lesson.id), entry["at"], entry["kind"],
                             pid_to_id(entry.get("participant", "")), data)

    def ip_of(request: Request) -> Optional[str]:
        return request.client.host if request.client else None

    def audit(request: Request, action: str, user: Optional[User] = None, obj: Optional[str] = None, **details):
        app.state.audit.log(action, user, obj, details or None, ip_of(request))

    @app.exception_handler(LessonError)
    async def lesson_error(request: Request, exc: LessonError):
        code = 403 if isinstance(exc, PermissionDenied) else 404 if isinstance(exc, NotFound) else 409
        return JSONResponse({"detail": str(exc)}, status_code=code)

    # ---------- кто спрашивает ----------
    def bearer(authorization: str) -> str:
        return authorization[7:].strip() if authorization[:7].lower() == "bearer " else ""

    async def session(request: Request, authorization: str = Header(default="")) -> User:
        token = bearer(authorization)
        user = app.state.accounts.session_user(token)
        if user is None:
            raise HTTPException(401, "нужен вход")
        request.state.token = token
        return user

    def require(*roles: str):
        async def dep(request: Request, user: User = Depends(session)) -> User:
            if user.must_change:
                raise HTTPException(403, "сначала смените временный пароль")
            if user.role not in roles:
                audit(request, "access.denied", user, request.url.path)
                raise HTTPException(403, "недостаточно прав")
            return user
        return dep

    teacher = require("teacher")
    student = require("student")
    admin = require("admin")
    member = require("teacher", "student")

    def stepup_ok(request: Request) -> bool:
        return stepups.get(request.state.token, 0) > clock()

    def need_stepup(request: Request) -> None:
        if not stepup_ok(request):
            raise HTTPException(428, "подтвердите действие паролем администратора")

    # ---------- представления ----------
    def lesson_dict(lesson_id: str) -> dict:
        rt = eng().get(lesson_id)
        return {"id": lesson_id, "state": rt.lesson.state.value, "owner": rt.lesson.owner_id, "live": True,
                "mode": "cards" if rt.cards_mode else "card_actions", "config": rt.config,
                "pack": rt.pack.summary(), "time_scale": rt.time_scale,
                "participants": [{"id": p, "name": rt.names.get(p, p),
                                  "role": s.role.service if s.role else None}
                                 for p, s in rt.lesson.seats.items()]}

    def stored_lesson_dict(row: dict) -> dict:
        pack = eng().packs.get(row["scenario"])
        summary = pack.summary() if pack else {"id": row["scenario"], "title": row["title"], "role_title": "",
                                               "cards": 0, "duration_s": 0}
        return {"id": "L%d" % row["id"], "state": row["state"], "owner": "u%d" % row["teacher_id"], "live": False,
                "mode": "cards" if row["mode"] == "карточки" else "card_actions", "config": row.get("config") or {},
                "pack": summary, "time_scale": (row.get("config") or {}).get("time_scale", 1.0), "participants": [],
                "created_at": row["created_at"], "finished_at": row["finished_at"]}

    def results_payload(lesson_row: dict, rows: List[dict]) -> dict:
        participants = []
        categories = []
        for r in rows:
            s = r["summary"]
            categories = s.get("categories") or categories
            final = r["expert_score"] if r["expert_score"] is not None else r["score"]
            crit = criteria_of(s.get("criteria"))
            participants.append(dict(s, participant="u%d" % r["user_id"], name=r["user_name"], role=r["role"],
                                     score=r["score"], findings=r["findings"], result_id=r["id"], final_score=final,
                                     expert_score=r["expert_score"], expert_comment=r["expert_comment"],
                                     **verdict(final, len(r["findings"]), crit)))
        return {"lesson": "L%d" % lesson_row["id"], "pack": lesson_row["title"], "categories": categories,
                "participants": participants}

    def own_lesson_row(user: User, lesson_id: str) -> dict:
        row = app.state.lessons.get(lesson_db_id(lesson_id))
        if row is None:
            raise HTTPException(404, "нет занятия %s" % lesson_id)
        if row["teacher_id"] != user.id:
            raise HTTPException(403, "чужое занятие")
        return row

    # ---------- служебное ----------
    @app.get("/api/health")
    async def health():
        """Состояние сервиса — для локального мониторинга, которого требует ТЗ."""
        st = app.state
        e = getattr(st, "engine", None)
        return {"status": "ok", "version": arm112.__version__, "core": arm112.core_info(),
                "routing": e.router.engine_name if e else None,
                "packs": len(e.packs) if e else 0,
                "database": getattr(getattr(st, "db", None), "dialect", None),
                "textcheck": bool(e and e.textcheck),
                "tts": st.tts.status() if hasattr(st, "tts") else None,
                "asr": st.asr.status() if hasattr(st, "asr") else None,
                "lessons_running": sum(rt.lesson.state is State.RUNNING for rt in e.lessons.values()) if e else 0,
                "connections": conns.count()}

    # ---------- вход ----------
    @app.post("/api/login")
    async def login(body: LoginIn, request: Request):
        try:
            # PBKDF2 (200 000 итераций) считается в пуле потоков: иначе вход 30 обучающихся в начале пары
            # на несколько секунд останавливает цикл событий — и занятия всех остальных (нагрузочный тест, узел 8)
            token, user = await asyncio.to_thread(app.state.accounts.authenticate, body.login, body.password, ip_of(request))
        except AuthError as exc:
            audit(request, "login.fail", None, None, login=body.login.strip().lower()[:40], reason=str(exc))
            raise HTTPException(401, str(exc))
        if app.state.settings.get("service.maintenance") and user.role == "student":
            app.state.accounts.logout(token)
            raise HTTPException(503, "идёт обслуживание системы — вход обучающихся временно закрыт")
        audit(request, "login.ok", user)
        return {"token": token, "user": user.to_dict()}

    @app.post("/api/logout")
    async def logout(request: Request, user: User = Depends(session)):
        app.state.accounts.logout(request.state.token)
        stepups.pop(request.state.token, None)
        audit(request, "logout", user)
        return {"ok": True}

    @app.get("/api/me")
    async def me(user: User = Depends(session)):
        return dict(user.to_dict(), role_title=ROLE_TITLES[user.role])

    @app.post("/api/password")
    async def change_password(body: PasswordIn, request: Request, user: User = Depends(session)):
        try:
            await asyncio.to_thread(app.state.accounts.change_password, user.id, body.old, body.new,
                                    keep_token=request.state.token)
        except AuthError as exc:
            audit(request, "password.fail", user)
            raise HTTPException(403, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        audit(request, "password.change", user)
        return dict(app.state.accounts.get(user.id).to_dict())

    # ---------- занятия ----------
    @app.get("/api/packs")
    async def packs(user: User = Depends(teacher)):
        out = []
        titles = eng().views.group_cards
        student_rows = app.state.student_cards.recent()
        for p in eng().packs.values():
            d = dict(p.summary(), mode=getattr(p, "mode", "card_actions"))
            if d["mode"] != "cards":  # сколько карточек обучающихся доходит до этой службы (ТЗ, сценарий 2)
                d["student_cards"] = len(gen.usable_student_rows(student_rows, p.raw, eng().router,
                                                                 eng().router.classifier))
            if d["mode"] == "cards":  # категории событий для выбора преподавателем (ТЗ: множественный выбор)
                d["groups"] = [{"group": g, "title": titles.get(g, {}).get("title", g), "count": n}
                               for g, n in sorted(p.groups(eng().router.classifier).items(), key=lambda x: int(x[0]))]
            out.append(d)
        return out

    @app.get("/api/lessons")
    async def lessons(user: User = Depends(member)):
        out, seen = [], set()
        my_groups = set() if user.is_teacher else {g["id"] for g in app.state.groups.of_student(user.id)}
        for lid, rt in eng().lessons.items():
            mine = rt.lesson.owner_id == user.pid if user.is_teacher else user.pid in rt.lesson.seats
            joinable = (not user.is_teacher and rt.lesson.state is State.RECRUITING and not rt.config.get("solo")
                        and (rt.config.get("group_id") is None or rt.config["group_id"] in my_groups))
            if mine or joinable:
                out.append(lesson_dict(lid))
                seen.add(lid)
        if user.is_teacher:  # история: завершённые и прерванные занятия из базы
            for row in app.state.lessons.for_teacher(user.id):
                if "L%d" % row["id"] not in seen:
                    out.append(stored_lesson_dict(row))
        return sorted(out, key=lambda d: lesson_db_id(d["id"]))

    def lesson_config(body: LessonIn) -> tuple:
        """Проверенные параметры занятия и его пакет. Общие для занятия преподавателя и задания (узел 6)."""
        if not 0.05 <= body.time_scale <= 3.0:  # 3 — замедление втрое: знакомство с АРМ без спешки
            raise HTTPException(400, "темп — от 0,05 (ускорение ×20) до 3 (замедление ×3)")
        pack = eng().packs.get(body.pack_id)
        if pack is None:
            raise HTTPException(404, "нет пакета сценариев %s" % body.pack_id)
        cards = getattr(pack, "mode", "") == "cards"
        checks = [(1 <= body.count <= 50, "вызовов на обучающегося — от 1 до 50"),
                  (body.feedback in ("immediate", "end"), "разбор — immediate или end"),
                  (body.norm_s is None or 20 <= body.norm_s <= 900, "норматив — от 20 до 900 с"),
                  (body.source in ("pack", "student", "mixed"), "источник карточек — pack, student или mixed"),
                  (body.pass_score is None or 0 <= body.pass_score <= 100, "порог зачёта — от 0 до 100 баллов"),
                  (body.max_errors is None or 0 <= body.max_errors <= 500, "допустимых замечаний — от 0 до 500"),
                  (body.primary_norm_s is None or 5 <= body.primary_norm_s <= 600, "норматив статуса — от 5 до 600 с"),
                  (body.answer_norm_s is None or 3 <= body.answer_norm_s <= 120, "норматив принятия вызова — от 3 до 120 с")]
        for ok, message in checks:
            if not ok:
                raise HTTPException(400, message)
        config = {"time_scale": body.time_scale}
        config.update({k: getattr(body, k) for k in ("pass_score", "max_errors", "text_check") if getattr(body, k) is not None})
        lesson_pack = pack
        if cards:
            config.update(count=body.count, feedback=body.feedback, norm_s=body.norm_s, groups=body.groups or None)
            if body.answer_norm_s is not None:
                config["answer_norm_s"] = body.answer_norm_s
        else:
            if body.primary_norm_s is not None:
                config["primary_norm_s"] = body.primary_norm_s
            if body.source != "pack":  # поток ДДС из карточек, заполненных обучающимися в режиме «карточки»
                try:
                    raw = gen.with_student_cards(pack.raw, app.state.student_cards.recent(), body.source, body.count,
                                                 eng().router, eng().router.classifier, seed="%s:%s" % (pack.id, clock()))
                    lesson_pack = eng().build_pack(raw)
                except ValueError as exc:
                    raise HTTPException(409, str(exc))
                app.state.tts.enqueue(pack_phrases(raw))  # доклады бригады по карточкам обучающихся
                config.update(source=body.source, count=body.count)
        if body.group_id is not None:
            config["group_id"] = body.group_id
        return pack, lesson_pack, config

    def open_lesson(owner_id: int, actor, pack, lesson_pack, config: dict):
        if app.state.settings.get("service.maintenance"):
            raise HTTPException(503, "идёт обслуживание системы — новые занятия временно не открываются")
        live = sum(rt.lesson.state in (State.RECRUITING, State.RUNNING) for rt in eng().lessons.values())
        if live >= app.state.settings.get("performance.max_lessons"):
            raise HTTPException(503, "открыто предельное число занятий (%d) — завершите одно из них" % live)
        cards = getattr(pack, "mode", "") == "cards"
        dbid = app.state.lessons.create(owner_id, "карточки" if cards else "действия с карточками", lesson_pack.id,
                                        lesson_pack.title, config, State.RECRUITING.value, group_id=config.get("group_id"))
        return eng().create_lesson(actor, pack.id, config["time_scale"], lesson_id="L%d" % dbid, config=config,
                                   pack=lesson_pack)

    @app.post("/api/lessons")
    async def create_lesson(body: LessonIn, request: Request, user: User = Depends(teacher)):
        if body.group_id is not None:
            group = app.state.groups.get(body.group_id)
            if group is None or group["teacher_id"] != user.id:
                raise HTTPException(404, "нет такой группы")
        pack, lesson_pack, config = lesson_config(body)
        rt = open_lesson(user.id, user.actor, pack, lesson_pack, config)
        audit(request, "lesson.create", user, rt.lesson.id, pack=pack.id, **config)
        return lesson_dict(rt.lesson.id)

    @app.post("/api/lessons/{lesson_id}/join")
    async def join(lesson_id: str, request: Request, user: User = Depends(student)):
        cfg = eng().get(lesson_id).config
        if cfg.get("solo") or (cfg.get("group_id") is not None and cfg["group_id"] not in
                               {g["id"] for g in app.state.groups.of_student(user.id)}):
            raise HTTPException(403, "это занятие для другой группы")
        await conns.deliver(eng().join(lesson_id, user.pid, user.name))
        audit(request, "lesson.join", user, lesson_id)
        return lesson_dict(lesson_id)

    @app.post("/api/lessons/{lesson_id}/roles")
    async def assign_role(lesson_id: str, body: RoleIn, request: Request, user: User = Depends(teacher)):
        await conns.deliver(eng().assign_role(user.actor, lesson_id, body.participant, body.service))
        audit(request, "lesson.role", user, lesson_id, participant=body.participant, service=body.service)
        return lesson_dict(lesson_id)

    @app.post("/api/lessons/{lesson_id}/start")
    async def start(lesson_id: str, request: Request, user: User = Depends(teacher)):
        await conns.deliver(eng().start(user.actor, lesson_id))
        app.state.lessons.set_state(lesson_db_id(lesson_id), State.RUNNING.value, started_at=clock())
        audit(request, "lesson.start", user, lesson_id)
        return lesson_dict(lesson_id)

    @app.post("/api/lessons/{lesson_id}/finish")
    async def finish(lesson_id: str, request: Request, user: User = Depends(member)):
        rt = eng().get(lesson_id)
        actor = user.actor
        if not user.is_teacher:  # самостоятельное задание обучающийся завершает сам — от имени владельца занятия
            if not (rt.config.get("solo") and user.pid in rt.lesson.seats):
                raise HTTPException(403, "завершить занятие может только преподаватель")
            actor = teaching.owner_actor(rt)
        await conns.deliver(eng().finish(actor, lesson_id))
        rt = eng().get(lesson_id)
        app.state.results.save(lesson_db_id(lesson_id), rt.results["participants"])
        if rt.cards_mode and rt.desk:  # карточки обучающихся пойдут в поток занятий ДДС
            app.state.student_cards.save(lesson_db_id(lesson_id), rt.desk.saved_cards())
        app.state.lessons.set_state(lesson_db_id(lesson_id), rt.lesson.state.value, finished_at=clock())
        audit(request, "lesson.finish", user, lesson_id, participants=len(rt.results["participants"]))
        return lesson_dict(lesson_id)

    @app.get("/api/lessons/{lesson_id}/results")
    async def results(lesson_id: str, user: User = Depends(member)):
        dbid = lesson_db_id(lesson_id)
        if user.is_teacher:
            row = own_lesson_row(user, lesson_id)
            rows = app.state.results.for_lesson(dbid)
            if not rows:
                raise HTTPException(409, "занятие ещё не завершено")
            return results_payload(row, rows)
        own = [r for r in app.state.results.for_user(user.id) if r["lesson_id"] == dbid]
        if not own:
            raise HTTPException(403, "нет ваших результатов в этом занятии")
        row = app.state.lessons.get(dbid)
        return results_payload(row, own)["participants"][0]

    @app.get("/api/results/me")
    async def my_results(user: User = Depends(student)):
        return [{"result_id": r["id"], "lesson": "L%d" % r["lesson_id"], "title": r["lesson_title"],
                 "mode": r["lesson_mode"], "finished_at": r["finished_at"], "score": r["score"],
                 "expert_score": r["expert_score"], "expert_comment": r["expert_comment"],
                 "findings": r["findings"], "summary": r["summary"]} for r in app.state.results.for_user(user.id)]

    @app.get("/api/results")
    async def teacher_results(user: User = Depends(teacher)):
        return [{"result_id": r["id"], "lesson": "L%d" % r["lesson_id"], "title": r["lesson_title"],
                 "student": r["user_name"], "student_id": r["user_id"], "finished_at": r["finished_at"],
                 "score": r["score"], "expert_score": r["expert_score"], "findings": r["findings"],
                 "summary": r["summary"]} for r in app.state.results.for_teacher(user.id)]

    # ---------- классификатор: опросная карта и маршрутизация (режим «карточки») ----------
    def classifier():
        return eng().router.classifier

    @app.get("/api/classifier/call-types")
    async def call_types(user: User = Depends(member)):
        """«Что случилось?»: 51 тип в порядке стенда; у типов-происшествий — группа классификатора."""
        with open(data_dir() / "classifier" / "call_types.json", encoding="utf-8") as f:
            types = json.load(f)["types"]
        by_list = {v["list"]: g for g, v in eng().views.group_cards.items()}
        return [{"name": t["name"], "kind": t["kind"], "group": by_list.get(t["name"])} for t in types]

    @app.get("/api/classifier/groups/{group}")
    async def classifier_group(group: str, user: User = Depends(member)):
        titles = eng().views.group_cards
        if group not in titles:
            raise HTTPException(404, "нет группы %s" % group)
        return questionnaire(classifier(), group, titles[group]["title"]).to_dict()

    @app.get("/api/classifier/search")
    async def classifier_search(q: str = "", user: User = Depends(member)):
        """Поиск типа происшествия по части слов в любом порядке (инструкция, раздел 4)."""
        words = [w for w in q.lower().replace("ё", "е").split() if len(w) >= 2]
        if not words:
            return []
        titles = eng().views.group_cards
        out = []
        for inc in classifier().incidents.values():
            if not inc["operator_visible"]:
                continue
            text = " ".join((inc["type"], inc["attr1"], inc["attr2"], inc["attr3"],
                             titles.get(inc["group"], {}).get("title", ""))).lower().replace("ё", "е")
            if all(w in text for w in words):
                out.append({"code": inc["code"], "type": inc["type"], "group": inc["group"],
                            "attr1": inc["attr1"], "attr2": inc["attr2"], "attr3": inc["attr3"],
                            "title": titles.get(inc["group"], {}).get("title", inc["group"])})
                if len(out) >= 30:
                    break
        return out

    @app.get("/api/route")
    async def route(code: str, flags: str = "", user: User = Depends(member)):
        """Службы по ЕКП для выбранного кода и признаков — список появляется, когда код определён."""
        if code not in classifier().incidents:
            raise HTTPException(404, "нет кода %s" % code)
        chosen = [f for f in flags.split(",") if f in classifier().flags]
        return [p.to_dict() for p in eng().router.plaques(code, chosen, None) if p.row == 1]

    @app.get("/api/services")
    async def services(user: User = Depends(member)):
        with open(data_dir() / "services" / "services.json", encoding="utf-8") as f:
            return [{"short": s["short"], "full": s["full"]} for s in json.load(f)["services"]]

    @app.post("/api/text/check")
    async def text_check(body: TextIn, user: User = Depends(member)):
        """Орфография и оформление текста — для проверки сценариев преподавателем (ТЗ: «проверка грамматики»)."""
        checker = eng().textcheck
        if checker is None:
            raise HTTPException(503, "проверка текста не установлена (нужен пакет pymorphy3)")
        return checker.check(body.text[:5000])

    # ---------- сценарии: генерация, проверка эталона, утверждение (узел 4) ----------
    def scenario_summary(row: dict) -> dict:
        body = row["body"]
        items = body.get("calls") if row["mode"] == "cards" else body.get("cards")
        return {"id": row["id"], "slug": row["slug"], "title": row["title"], "mode": row["mode"], "role": row["role"],
                "role_title": body.get("role_title") or "Оператор Службы 112", "difficulty": row["difficulty"],
                "categories": row["categories"], "status": row["status"], "source": row["source"],
                "cards": len(items or []), "author": row.get("author"), "author_id": row["author_id"],
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def scenario_full(row: dict) -> dict:
        titles = eng().views.group_cards

        def qfn(group):
            return questionnaire(classifier(), group, titles.get(group, {}).get("title", group)).to_dict()
        return dict(scenario_summary(row), reference=row["body"].get("reference"),
                    items=gen.preview(row["body"], classifier(), qfn, eng().textcheck))

    def scenario_row(sid: int) -> dict:
        row = app.state.scenarios.get(sid)
        if row is None:
            raise HTTPException(404, "нет сценария %s" % sid)
        return row

    def own_scenario(user: User, sid: int, draft_only: bool = False) -> dict:
        row = scenario_row(sid)
        if row["author_id"] != user.id:
            raise HTTPException(403, "сценарий другого преподавателя: его можно посмотреть или скопировать")
        if draft_only and row["status"] != DRAFT:
            raise HTTPException(409, "править можно только черновик — сделайте копию сценария")
        return row

    @app.get("/api/scenarios/options")
    async def scenario_options(user: User = Depends(teacher)):
        titles = eng().views.group_cards

        def title(g):
            return titles.get(g, {}).get("title", g)
        return {"districts": [{"name": d, "okrug": v["okrug"]} for d, v in app.state.gazetteer.districts.items()],
                "dds_groups": [{"group": g, "title": title(g)} for g in gen.dds_groups()],
                "call_groups": [{"group": g, "title": title(g)} for g in gen.call_groups()],
                "levels": [{"level": k, "title": v} for k, v in gen.LEVEL_TITLE.items()],
                "llm": app.state.llm.available()}

    @app.get("/api/scenarios")
    async def scenarios_list(user: User = Depends(teacher)):
        return [scenario_summary(r) for r in app.state.scenarios.list()]

    @app.post("/api/scenarios/generate")
    async def scenarios_generate(body: GenerateIn, request: Request, user: User = Depends(teacher)):
        if body.mode not in ("card_actions", "cards"):
            raise HTTPException(400, "режим — card_actions или cards")
        try:
            if body.mode == "cards":
                raw = gen.generate_calls(app.state.gazetteer, body.groups or None, body.difficulty, body.count, body.seed)
            else:
                raw = gen.generate_dds(eng().router, app.state.gazetteer, body.district or "", body.difficulty,
                                       body.count, body.groups or None, body.seed)
            eng().build_pack(raw)  # тот же контроль, что у пакетов из файлов
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        sid = app.state.scenarios.create(raw, body.mode, raw.get("role"), body.difficulty, body.groups or [],
                                         "generated", user.id)
        audit(request, "scenario.generate", user, "sc-%d" % sid, mode=body.mode, district=body.district,
              difficulty=body.difficulty, count=body.count, groups=body.groups or [], seed=raw["generated"]["seed"])
        return scenario_full(scenario_row(sid))

    @app.get("/api/scenarios/{sid}")
    async def scenario_get(sid: int, user: User = Depends(teacher)):
        return scenario_full(scenario_row(sid))

    @app.post("/api/scenarios/{sid}/cards/{key}/revise")
    async def scenario_revise(sid: int, key: str, body: ReviseIn, request: Request, user: User = Depends(teacher)):
        row = own_scenario(user, sid, draft_only=True)
        comment = body.comment.strip()[:1000]
        try:
            new, changes = gen.revise_rules(row["body"], key, comment, eng().router, app.state.gazetteer)
            if not changes and app.state.llm.available():  # модель — в отдельном потоке: занятия не ждут
                text = await asyncio.to_thread(app.state.llm.rewrite, gen.item_text(new, key), comment)
                new, changes = gen.revise_text(new, key, text, eng().textcheck)
            new, changes = gen.finish_revision(new, key, comment, changes)
            eng().build_pack(new)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        app.state.scenarios.update(sid, new)
        audit(request, "scenario.revise", user, row["slug"], card=key, comment=comment[:200], changes=changes)
        return dict(scenario_full(scenario_row(sid)), changes=changes)

    @app.post("/api/scenarios/{sid}/cards/{key}/exclude")
    async def scenario_exclude(sid: int, key: str, request: Request, user: User = Depends(teacher)):
        row = own_scenario(user, sid, draft_only=True)
        try:
            new = gen.exclude(row["body"], key)
            eng().build_pack(new)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        app.state.scenarios.update(sid, new)
        audit(request, "scenario.exclude", user, row["slug"], card=key)
        return scenario_full(scenario_row(sid))

    @app.post("/api/scenarios/{sid}/approve")
    async def scenario_approve(sid: int, body: ApproveIn, request: Request, user: User = Depends(teacher)):
        row = own_scenario(user, sid, draft_only=True)
        try:
            new = gen.keep_only(row["body"], body.keys) if body.keys is not None else row["body"]
            if body.title and body.title.strip():
                new = dict(new, title=body.title.strip()[:200])
            pack = eng().build_pack(new)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        app.state.scenarios.update(sid, new, status=APPROVED)
        eng().packs[pack.id] = pack
        app.state.tts.enqueue(pack_phrases(new))  # озвучка утверждённых реплик — фоном, пока собирается группа
        items = new.get("calls") if row["mode"] == "cards" else new.get("cards")
        audit(request, "scenario.approve", user, row["slug"], cards=len(items), partial=body.keys is not None)
        return scenario_full(scenario_row(sid))

    @app.post("/api/scenarios/{sid}/archive")
    async def scenario_archive(sid: int, request: Request, user: User = Depends(teacher)):
        row = own_scenario(user, sid)
        if row["status"] == ARCHIVED:
            raise HTTPException(409, "сценарий уже в архиве")
        app.state.scenarios.set_status(sid, ARCHIVED)
        eng().packs.pop(row["slug"], None)  # идущие занятия доиграют: пакет у них уже в памяти
        audit(request, "scenario.archive", user, row["slug"])
        return scenario_full(scenario_row(sid))

    @app.post("/api/scenarios/import")
    async def scenario_import(request: Request, user: User = Depends(teacher)):
        """Импорт пакета сценариев из файла JSON (формат data/scenarios) — черновиком, с той же проверкой.
        ТЗ: «механизм импорта обновлений: пакетное обновление учебных материалов и сценариев»."""
        try:
            raw = await request.json()
            if not isinstance(raw, dict) or not (raw.get("cards") or raw.get("calls")):
                raise ValueError("в файле нет карточек (cards) или вызовов (calls)")
            raw.setdefault("title", "Импортированный сценарий")
            raw.setdefault("id", "import")
            eng().build_pack(raw)
        except ValueError as exc:
            raise HTTPException(400, "сценарий не принят: %s" % exc)
        mode = "cards" if raw.get("mode") == "cards" else "card_actions"
        difficulty = int((raw.get("generated") or {}).get("difficulty") or raw.get("difficulty") or 1)
        sid = app.state.scenarios.create(raw, mode, raw.get("role"), difficulty, [], "import", user.id)
        audit(request, "scenario.import", user, "sc-%d" % sid, title=raw["title"][:100])
        return scenario_full(scenario_row(sid))

    @app.post("/api/scenarios/{sid}/copy")
    async def scenario_copy(sid: int, request: Request, user: User = Depends(teacher)):
        """Копия в черновик: утверждённый сценарий не меняется под идущими занятиями — правят копию."""
        row = scenario_row(sid)
        body = dict(row["body"], title=(row["title"] + " (копия)")[:200])
        new_sid = app.state.scenarios.create(body, row["mode"], row["role"], row["difficulty"], row["categories"],
                                             "copy", user.id)
        audit(request, "scenario.copy", user, "sc-%d" % new_sid, of=row["slug"])
        return scenario_full(scenario_row(new_sid))

    # ---------- администратор ----------
    @app.post("/api/admin/confirm")
    async def admin_confirm(body: ConfirmIn, request: Request, user: User = Depends(admin)):
        if not await asyncio.to_thread(app.state.accounts.check_password, user.id, body.password):
            audit(request, "admin.confirm_fail", user)
            raise HTTPException(403, "пароль указан неверно")
        stepups[request.state.token] = clock() + STEPUP_S
        audit(request, "admin.confirm", user)
        return {"ok": True, "valid_s": STEPUP_S}

    @app.get("/api/admin/users")
    async def admin_users(user: User = Depends(admin)):
        return app.state.accounts.list()

    @app.post("/api/admin/users")
    async def admin_create_user(body: UserIn, request: Request, user: User = Depends(admin)):
        need_stepup(request)
        try:
            created, password = await asyncio.to_thread(app.state.accounts.create, body.login, body.name, body.role)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        audit(request, "user.create", user, created.login, role=created.role)
        return {"user": created.to_dict(), "temporary_password": password}

    @app.patch("/api/admin/users/{user_id}")
    async def admin_update_user(user_id: int, body: UserPatch, request: Request, user: User = Depends(admin)):
        need_stepup(request)
        target = app.state.accounts.get(user_id)
        if target is None:
            raise HTTPException(404, "нет такой учётной записи")
        if user_id == user.id and (body.blocked or (body.role and body.role != "admin")):
            raise HTTPException(409, "нельзя заблокировать себя или снять с себя роль администратора")
        try:
            if body.name is not None or body.role is not None:
                app.state.accounts.update(user_id, body.name, body.role)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if body.blocked is not None:
            app.state.accounts.set_blocked(user_id, body.blocked)
            audit(request, "user.block" if body.blocked else "user.unblock", user, target.login)
        if body.name is not None or body.role is not None:
            audit(request, "user.update", user, target.login, name=body.name, role=body.role)
        return app.state.accounts.get(user_id).to_dict()

    @app.post("/api/admin/users/{user_id}/reset-password")
    async def admin_reset_password(user_id: int, request: Request, user: User = Depends(admin)):
        need_stepup(request)
        target = app.state.accounts.get(user_id)
        if target is None:
            raise HTTPException(404, "нет такой учётной записи")
        password = await asyncio.to_thread(app.state.accounts.reset_password, user_id)
        audit(request, "user.reset_password", user, target.login)
        return {"temporary_password": password}

    @app.get("/api/admin/audit")
    async def admin_audit(limit: int = 200, action: str = "", login: str = "", before: int = 0,
                          user: User = Depends(admin)):
        return app.state.audit.list(limit, action or None, login or None, before or None)

    @app.get("/api/admin/audit/verify")
    async def admin_audit_verify(request: Request, user: User = Depends(admin)):
        result = app.state.audit.verify()
        audit(request, "audit.verify", user, None, ok=result["ok"], checked=result["checked"])
        return result

    # ---------- WebSocket занятия ----------
    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket, lesson: str = ""):
        # Токен приходит первым сообщением, а не в адресе: адрес попадает в журналы доступа.
        await websocket.accept()
        try:
            hello = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=HELLO_TIMEOUT_S))
        except WebSocketDisconnect:
            return
        except (asyncio.TimeoutError, ValueError):
            hello = None
        token = hello.get("token") if isinstance(hello, dict) and hello.get("t") == "hello" else None
        user = app.state.accounts.session_user(token)
        if user is None or user.must_change:
            await websocket.close(code=4401)
            return
        if lesson not in eng().lessons:
            await websocket.close(code=4404)
            return
        rt = eng().get(lesson)
        if user.is_teacher:
            allowed = rt.lesson.owner_id == user.pid
        elif user.role == "student":
            allowed = user.pid in rt.lesson.seats
        else:
            allowed = False  # администратор не вмешивается в учебный процесс (ТЗ)
        if not allowed:
            await websocket.close(code=4403)
            return
        target = ("teacher:" + user.pid) if user.is_teacher else user.pid
        conns.add(target, websocket)
        try:
            if user.is_teacher:
                outs = [(target, eng().monitor(lesson))]
                if rt.results:
                    outs.append((target, {"t": "results", "results": rt.results}))
            else:
                outs = eng().connect(lesson, user.pid) or [(target, eng().snapshot(lesson, user.pid))]
            await conns.deliver(outs)
            while True:
                text = await websocket.receive_text()
                if user.is_teacher:
                    continue  # преподаватель управляет занятием через REST, сокет — только монитор
                try:
                    msg = json.loads(text)
                    outs = eng().handle(lesson, user.pid, msg if isinstance(msg, dict) else {})
                except (ValueError, TypeError):
                    outs = [(target, {"t": "error", "reason": "неразборчивое сообщение"})]
                await conns.deliver(outs)
        except WebSocketDisconnect:
            pass
        finally:
            conns.remove(target, websocket)
            if not user.is_teacher and not conns.has(target):
                await conns.deliver(eng().disconnect(lesson, user.pid))

    # ---------- микрофон оператора 112 (узел 9) ----------
    @app.websocket("/ws/voice")
    async def ws_voice(websocket: WebSocket, lesson: str = ""):
        """Звук микрофона (PCM 16 кГц, 16 бит, моно) → промежуточный текст и итог фразы с вопросом.
        Вопрос в занятие отправляет сама страница — тем же сообщением «ask», что и кнопки."""
        await websocket.accept()
        try:
            hello = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=HELLO_TIMEOUT_S))
        except WebSocketDisconnect:
            return
        except (asyncio.TimeoutError, ValueError):
            hello = None
        token = hello.get("token") if isinstance(hello, dict) and hello.get("t") == "hello" else None
        user = app.state.accounts.session_user(token)
        if user is None or user.must_change:
            await websocket.close(code=4401)
            return
        rt = eng().lessons.get(lesson)
        if rt is None or user.role != "student" or user.pid not in rt.lesson.seats:
            await websocket.close(code=4403)
            return
        try:
            stream = await app.state.asr.open()
        except AsrUnavailable as exc:
            await websocket.send_json({"t": "unavailable", "reason": str(exc)})
            await websocket.close(code=4503)
            return
        opened, heard = time.monotonic(), 0.0
        try:
            await websocket.send_json({"t": "ready", "rate": ASR_RATE})
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                out = None
                if msg.get("bytes") is not None:
                    data = msg["bytes"]
                    heard += len(data) / 2 / ASR_RATE
                    if len(data) <= VOICE_CHUNK_MAX and heard <= time.monotonic() - opened + VOICE_AHEAD_S:
                        out = await stream.feed(data)
                elif msg.get("text"):
                    try:
                        cmd = json.loads(msg["text"])
                    except ValueError:
                        cmd = None
                    if isinstance(cmd, dict) and cmd.get("t") == "end":
                        out = await stream.end()
                if out:
                    if out["t"] == "final":
                        out["intent"] = detect_intent(out["text"])
                    await websocket.send_json(out)
        except (WebSocketDisconnect, RuntimeError):
            pass  # страница закрыта или связь оборвалась — поток распознавания освобождается
        finally:
            stream.close()

    @app.middleware("http")
    async def observe(request: Request, call_next):
        """Запросы API — в счётчики мониторинга и системный журнал; сбои (5xx) — в отчёт об ошибках."""
        t0 = time.perf_counter()
        path = request.url.path
        try:
            response = await call_next(request)
        except Exception as exc:
            st = app.state
            if hasattr(st, "syslog"):
                st.syslog.failure("http", "%s %s: %s: %s" % (request.method, path, type(exc).__name__, exc),
                                  path=path, error=type(exc).__name__)
                st.monitor.requests.add("сбой", 500, (time.perf_counter() - t0) * 1000)
            raise
        st = app.state
        if path.startswith("/api/") and hasattr(st, "monitor"):
            ms = (time.perf_counter() - t0) * 1000
            route = getattr(request.scope.get("route"), "path", path)
            st.monitor.requests.add("%s %s" % (request.method, route), response.status_code, ms)
            st.syslog.request(request.method, route, response.status_code, ms, None)
            if response.status_code >= 500:
                st.syslog.failure("http", "%s %s — ответ %d" % (request.method, route, response.status_code), path=route)
        return response

    @app.middleware("http")
    async def no_stale_pages(request: Request, call_next):
        # Экраны всегда сверяются с сервером (ответ 304, если не менялись): после обновления
        # в классе ни у кого не останется старая версия страницы из кэша браузера.
        response = await call_next(request)
        if request.url.path.startswith("/tts/"):  # имя файла — хеш содержимого: звук можно не перепроверять
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        response.headers.setdefault("Cache-Control", "no-cache")
        if request.url.scheme == "https":  # браузер больше не пойдёт на сервер по открытому HTTP
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    teaching.install(app, types.SimpleNamespace(
        eng=eng, teacher=teacher, student=student, member=member, audit=audit, clock=clock, conns=conns,
        lesson_dict=lesson_dict, lesson_db_id=lesson_db_id, own_lesson_row=own_lesson_row,
        lesson_config=lesson_config, open_lesson=open_lesson, LessonIn=LessonIn,
        materials_dir=materials_path))
    ops_ctx = types.SimpleNamespace(
        eng=eng, admin=admin, audit=audit, need_stepup=need_stepup, conns=conns, repo_root=REPO_ROOT,
        integrity_file=var_path / "integrity.json",
        scenarios_dir=Path(os.environ.get("A112_CONTENT_DIR") or data_dir() / "scenarios"),
        enqueue_tts=lambda raw: app.state.tts.enqueue(pack_phrases(raw)))
    admin_ops.install(app, ops_ctx)
    app.mount("/tts", StaticFiles(directory=str(tts_path)), name="tts")
    web = REPO_ROOT / "web"
    if web.is_dir():
        app.mount("/", StaticFiles(directory=str(web), html=True), name="web")
    return app


app = create_app()
