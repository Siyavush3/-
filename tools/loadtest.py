"""Нагрузочный тест тренажёра по требованиям ТЗ.

    python tools/loadtest.py [--users 100] [--sessions 20] [--port 8130] [--drops 10] [--voice 20] [--cpus 4] [--keep]

Что проверяется (ТЗ, «Требования к производительности»):
  - время отклика интерфейса не более 2 с при нагрузке до 100 пользователей;
  - не менее 20 одновременных сессий;
  - скорость записи в БД не менее 100 операций в секунду;
  - время формирования аналитических отчётов не более 30 с;
  - кратковременные сбои сети (до 30 с) — восстановление сессии без потери данных.

Сервер поднимается отдельно, на временной базе и временном каталоге (рабочие данные не трогаются).
Половина сессий — «действия с карточками» (ДДС, ускорение ×10), половина — «карточки» (оператор 112).
Обучающиеся и преподаватели — асинхронные клиенты по настоящему HTTP и WebSocket, как браузер:
токен первым сообщением, команды с номером seq, после обрыва — досылка неподтверждённых команд.
Операторы 112 (--voice) ещё и говорят в микрофон: фразы оператора голосами piper в реальном времени
по /ws/voice, как браузер; распознанный вопрос уходит заявителю. Замеряется задержка итога и точность.
Итог — отчёт build/loadtest/report-<время>.md и .json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "pycore")]

import websockets  # noqa: E402
from domain.calls import detect_intent  # noqa: E402

PASSWORD = "Load-test-112"
VOICE_PHRASES = [("Служба сто двенадцать, слушаю. Что у вас случилось?", "details"), ("Назовите адрес, пожалуйста.", "address"),
                 ("Уточните номер дома и подъезд.", "address_clarified"), ("Как вас зовут?", "name"),
                 ("Кем вы приходитесь пострадавшему?", "status"), ("Есть пострадавшие?", "victims"),
                 ("Есть угроза людям?", "danger"), ("Как проехать к вашему дому?", "access"),
                 ("Оставьте номер телефона для связи.", "phone"), ("Хорошо, оставайтесь на линии.", None)]
VOICE = {"phrases": 0, "finals": 0, "correct": 0, "missing": 0, "speakers": 0, "unavailable": 0, "reason": ""}


class Stats:
    def __init__(self):
        self.lat = defaultdict(list)   # группа → [мс]
        self.errors = defaultdict(int)
        self.notes = []

    def add(self, group: str, ms: float, ok: bool = True):
        self.lat[group].append(ms)
        if not ok:
            self.errors[group] += 1

    def table(self):
        out = []
        for g, xs in sorted(self.lat.items()):
            xs = sorted(xs)
            out.append({"group": g, "n": len(xs), "p50": round(statistics.median(xs), 1),
                        "p95": round(xs[min(len(xs) - 1, int(0.95 * len(xs)))], 1), "max": round(xs[-1], 1),
                        "errors": self.errors.get(g, 0)})
        return out


STATS = Stats()


async def http(port, method, path, body=None, token=None, group=None):
    """Минимальный HTTP/1.1-клиент на asyncio: соединение на запрос, JSON туда и обратно."""
    t0 = time.perf_counter()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    data = b"" if body is None else json.dumps(body).encode("utf-8")
    head = ["%s %s HTTP/1.1" % (method, path), "Host: 127.0.0.1", "Connection: close",
            "Content-Type: application/json", "Content-Length: %d" % len(data)]
    if token:
        head.append("Authorization: Bearer " + token)
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode("ascii") + data)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    header, _, payload = raw.partition(b"\r\n\r\n")
    status = int(header.split(b" ", 2)[1])
    ms = (time.perf_counter() - t0) * 1000
    STATS.add(group or path.split("?")[0], ms, status < 500)
    try:
        return status, json.loads(payload.decode("utf-8")) if payload else None
    except ValueError:
        return status, None


def free_port(preferred: int) -> int:
    for port in (preferred, 0):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            return s.getsockname()[1]
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("нет свободного порта")


def seed(db_url: str, users: int, teachers: int) -> None:
    from app.auth import Accounts
    from store.db import Database
    from store.schema import migrate
    db = Database(db_url)
    migrate(db)
    acc = Accounts(db)
    acc.create("admin", "Администратор", "admin", PASSWORD, must_change=False)
    for i in range(1, teachers + 1):
        acc.create("t%02d" % i, "Преподаватель %d" % i, "teacher", PASSWORD, must_change=False)
    for i in range(1, users + 1):
        acc.create("s%03d" % i, "Обучающийся %03d" % i, "student", PASSWORD, must_change=False)
    db.close()


# ---------- обучающийся ----------
class Student:
    def __init__(self, port, login, lesson, mode, drop):
        self.port, self.login, self.lesson, self.mode, self.drop = port, login, lesson, mode, drop
        self.token = None
        self.pid = None
        self.seq = 0
        self.pending = {}          # seq → команда: досылаются после обрыва, как в браузере (sessionStorage)
        self.sent_at = {}
        self.acked = set()
        self.done = asyncio.Event()
        self.result = None
        self.reconnects = 0
        self.statuses_sent = 0
        self.resent = set()        # команды, досланные после восстановления связи (их задержка включает обрыв)
        self.ready = asyncio.Event()
        self.ws = None             # текущий сокет занятия: туда уходит вопрос, распознанный из речи
        self.call = None

    async def start(self, delay=0.0):
        await asyncio.sleep(delay)  # обучающиеся приходят в течение первых секунд пары, а не в одну миллисекунду
        st, body = await http(self.port, "POST", "/api/login", {"login": self.login, "password": PASSWORD}, group="POST /api/login")
        self.token, self.pid = body["token"], "u%d" % body["user"]["id"]
        await http(self.port, "POST", "/api/lessons/%s/join" % self.lesson, token=self.token, group="POST /join")
        self.ready.set()

    def send_later(self, ws, msg):
        self.seq += 1
        msg["seq"] = self.seq
        self.pending[self.seq] = msg
        self.sent_at[self.seq] = time.perf_counter()
        return self._send(ws, msg)

    async def _send(self, ws, msg):
        try:
            await ws.send(json.dumps(msg))
        except Exception:
            pass  # связь оборвалась — команда в pending, уйдёт после восстановления

    async def run(self, deadline):
        drop_at = time.time() + random.uniform(15, 40) if self.drop else None
        while time.time() < deadline and not self.done.is_set():
            try:
                async with websockets.connect("ws://127.0.0.1:%d/ws?lesson=%s" % (self.port, self.lesson),
                                              open_timeout=10) as ws:
                    await ws.send(json.dumps({"t": "hello", "token": self.token}))
                    for seq in sorted(self.pending):  # досылка неподтверждённых команд после восстановления связи
                        self.resent.add(seq)
                        await self._send(ws, self.pending[seq])
                    await self._loop(ws, deadline, drop_at)
                    if drop_at and time.time() >= drop_at and not self.done.is_set():
                        gap = random.uniform(10, 25)  # ТЗ: сбои сети до 30 секунд
                        drop_at = None
                        self.reconnects += 1
                        STATS.notes.append("%s: обрыв связи на %.0f с" % (self.login, gap))
                        await asyncio.sleep(gap)
            except (OSError, websockets.WebSocketException):
                await asyncio.sleep(1)

    async def _loop(self, ws, deadline, drop_at):
        self.ws = ws
        while time.time() < deadline:
            if drop_at and time.time() >= drop_at:
                self.ws = None
                await ws.close()  # «выдернули кабель»
                return
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
            except asyncio.TimeoutError:
                continue
            t = msg.get("t")
            if t in ("status_ack", "save_ack"):
                seq = msg.get("seq")
                if seq in self.pending:
                    group = "WS " + t + (" после обрыва" if seq in self.resent else "")
                    STATS.add(group, (time.perf_counter() - self.sent_at[seq]) * 1000)
                    self.pending.pop(seq, None)
                    self.acked.add(seq)
            elif t in ("status_rejected", "save_rejected"):
                self.pending.pop(msg.get("seq"), None)
                STATS.errors["WS " + t] += 1
            elif t == "result":
                self.result = msg["result"]
                self.done.set()
                return
            elif self.mode == "dds":
                asyncio.create_task(self._dds(ws, msg))
            else:
                asyncio.create_task(self._cards(ws, msg))

    async def _dds(self, ws, msg):
        if msg["t"] == "card":
            c = msg["card"]
            await asyncio.sleep(random.uniform(1, 5))
            self.statuses_sent += 1
            await self.send_later(ws, {"t": "set_status", "card": c["number"], "status": "Принята",
                                       "comment": "принята, направлена бригада"})
        elif msg["t"] == "call":
            await asyncio.sleep(random.uniform(0.5, 2))
            status = {"Начало": "Начало реагирования"}.get(msg["text"].split()[0])
            for word, st in (("на месте", "Прибытие"), ("меня", "Проведение работ"), ("перекры", "Проведение работ"),
                             ("завершен", "Работы завершены"), ("устранен", "Работы завершены")):
                if word in msg["text"].lower():
                    status = st
            if status:
                self.statuses_sent += 1
                await self.send_later(ws, {"t": "set_status", "card": msg["card"], "status": status, "comment": msg["text"][:80]})

    async def _cards(self, ws, msg):
        if msg["t"] == "incoming" or (msg["t"] == "state" and (msg.get("desk") or {}).get("current")
                                      and not msg["desk"]["current"].get("answered_at")):
            call = msg["call"] if msg["t"] == "incoming" else msg["desk"]["current"]
            self.call = call["number"]
            await asyncio.sleep(random.uniform(1, 2))
            await self._send(ws, {"t": "answer"})   # связь могла оборваться (имитация сбоя) — как в браузере, без падения
            for intent in ("address", "name", "victims"):
                await asyncio.sleep(random.uniform(0.5, 1.5))
                await self._send(ws, {"t": "ask", "intent": intent})
            await asyncio.sleep(random.uniform(4, 8))
            card = {"number": self.call, "code": "1010101", "flags": [], "victims": False,
                    "caller": {"name": "Иванов Иван", "status": "очевидец"},
                    "address": {"subject": "Москва", "street": "улица Рогова", "house": "5"},
                    "description": "Возгорание мусорного контейнера, пострадавших нет."}
            await self.send_later(ws, {"t": "save_card", "card": card})


# ---------- голос оператора ----------
def voice_clips():
    """Фразы оператора голосами piper, 16 кГц, 16 бит — как с микрофона. None — голос не проверить."""
    try:
        import numpy as np
        from speech.asr import MODEL, SPEECH_RMS, loudness
        from speech.dsp import resample
        from speech.synth import PiperSynth
    except ImportError as exc:
        return None, "нет пакетов озвучки или распознавания (%s)" % exc
    if not (ROOT / "data" / "asr" / MODEL / "am" / "final.mdl").is_file():
        return None, "нет модели распознавания (python tools/fetch_asr.py)"
    voices = sorted(p.name[:-5] for p in (ROOT / "data" / "voices").glob("*.onnx"))[:2]
    if not voices:
        return None, "нет голосов piper для фраз оператора (python tools/fetch_voices.py)"
    synth = PiperSynth(ROOT / "data" / "voices", threads=4)
    clips = []
    for voice in voices:
        for text, intent in VOICE_PHRASES:
            audio, rate = synth.synthesize(text, voice)
            speech = np.clip(resample(audio, rate, 16000) * 32767, -32768, 32767).astype("<i2").tobytes()
            lead = b"\0" * 6400                              # 0,2 с тишины до фразы
            pcm = lead + speech + b"\0" * 22400                # 0,7 с после
            # конец речи — последний громкий кусок 0,1 с (в синтезе после речи ещё есть своя тишина)
            end = max(i + 3200 for i in range(0, len(pcm), 3200) if loudness(pcm[i:i + 3200]) >= SPEECH_RMS)
            clips.append((text, intent, pcm, end))
    return clips, ""


class Speaker:
    """Оператор говорит в микрофон: фразы в реальном времени, пауза — слушает заявителя и заполняет карточку."""

    def __init__(self, student, clips):
        self.s, self.clips = student, clips

    async def run(self, deadline):
        s = self.s
        await s.ready.wait()
        finals = asyncio.Queue()
        try:
            async with websockets.connect("ws://127.0.0.1:%d/ws/voice?lesson=%s" % (s.port, s.lesson), open_timeout=10) as ws:
                await ws.send(json.dumps({"t": "hello", "token": s.token}))
                first = json.loads(await asyncio.wait_for(ws.recv(), 20))
                if first.get("t") != "ready":
                    VOICE["unavailable"] += 1
                    VOICE["reason"] = first.get("reason", "")
                    return
                VOICE["speakers"] += 1

                async def receiver():
                    async for raw in ws:
                        m = json.loads(raw)
                        if m.get("t") == "final":
                            finals.put_nowait((time.perf_counter(), m))
                rx = asyncio.create_task(receiver())
                k = random.randrange(len(self.clips))
                await asyncio.sleep(random.uniform(3, 10))
                while time.time() < deadline and not s.done.is_set():
                    text, intent, pcm, speech_end = self.clips[k % len(self.clips)]
                    k += 1
                    while not finals.empty():                      # итоги прошлой фразы сюда не относятся
                        finals.get_nowait()
                    t0 = time.perf_counter()
                    ended = None
                    for i in range(0, len(pcm), 3200):             # куски по 0,1 с в реальном времени
                        await ws.send(pcm[i:i + 3200])
                        if ended is None and i + 3200 >= speech_end:
                            ended = time.perf_counter()
                        await asyncio.sleep(max(0.0, t0 + (i + 3200) / 32000 - time.perf_counter()))
                    VOICE["phrases"] += 1
                    # итогов у фразы может быть несколько: пауза внутри («Служба 112, слушаю. Что случилось?»)
                    texts, last = [], None
                    until = time.perf_counter() + 1.5
                    while True:
                        try:
                            at, m = await asyncio.wait_for(finals.get(), timeout=max(0.05, until - time.perf_counter()))
                        except asyncio.TimeoutError:
                            break
                        texts.append(m["text"])
                        last = at
                        if s.ws is not None and m.get("intent"):          # вопрос уходит заявителю, как с кнопки
                            try:
                                await s.ws.send(json.dumps({"t": "ask", "intent": m["intent"], "text": m["text"], "via": "voice"}))
                            except Exception:
                                pass
                        if at >= ended:                                   # итог после конца речи — фраза закончена
                            break
                    if not texts:
                        VOICE["missing"] += 1
                        await ws.send(json.dumps({"t": "end"}))
                        await asyncio.sleep(0.5)
                    else:
                        VOICE["finals"] += 1
                        VOICE["correct"] += detect_intent(" ".join(texts)) == intent
                        STATS.add("голос: итог после конца речи", max(0.0, last - ended) * 1000)
                    await asyncio.sleep(random.uniform(2, 6))       # слушает ответ, заполняет карточку
                rx.cancel()
        except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as exc:
            VOICE["unavailable"] += 1
            VOICE["reason"] = type(exc).__name__


# ---------- преподаватель ----------
async def teacher_flow(port, login, specs, students, results, monitor_lessons):
    st, body = await http(port, "POST", "/api/login", {"login": login, "password": PASSWORD}, group="POST /api/login")
    token = body["token"]
    lessons = []
    for mode in specs:
        body = {"pack_id": "dds-shchukino-v0", "time_scale": 0.1} if mode == "dds" else \
            {"pack_id": "calls-bilety-v1", "count": 3, "norm_s": 80}
        st, l = await http(port, "POST", "/api/lessons", body, token, group="POST /api/lessons")
        lessons.append((l["id"], mode))
    return token, lessons


async def poll(port, token, path, stop, period, group):
    while not stop.is_set():
        await http(port, "GET", path, token=token, group=group)
        await asyncio.sleep(period)


async def main_async(a, port, tmp, clips=None):
    random.seed(112)
    stop = asyncio.Event()
    teachers = max(1, a.sessions // 4)
    per = a.users // a.sessions
    # 1) преподаватели открывают занятия: половина ДДС, половина 112
    modes = ["dds" if i % 2 == 0 else "cards" for i in range(a.sessions)]
    tokens, lessons = {}, []
    for i in range(teachers):
        chunk = modes[i * 4:(i + 1) * 4] if i < teachers - 1 else modes[i * 4:]
        tok, ls = await teacher_flow(port, "t%02d" % (i + 1), chunk, None, None, None)
        tokens["t%02d" % (i + 1)] = tok
        lessons += [(lid, mode, "t%02d" % (i + 1)) for lid, mode in ls]
    # 2) обучающиеся входят и записываются — все разом, как в начале пары
    students = []
    for n, (lid, mode, t) in enumerate(lessons):
        for k in range(per):
            idx = n * per + k + 1
            students.append((Student(port, "s%03d" % idx, lid, mode, drop=idx <= a.drops), t))
    # залп: все входят в одну и ту же секунду — отдельный замер, для сведения (пароль проверяется намеренно медленно)
    t0 = time.perf_counter()
    await asyncio.gather(*(http(port, "POST", "/api/login", {"login": s.login, "password": PASSWORD},
                                group="вход залпом (для сведения)") for s, _ in students))
    login_s = time.perf_counter() - t0
    await asyncio.gather(*(s.start(random.uniform(0, a.arrival)) for s, _ in students))
    # 3) роли и старт
    for lid, mode, t in lessons:
        st, l = await http(port, "GET", "/api/lessons", token=tokens[t], group="GET /api/lessons")
        info = next(x for x in l if x["id"] == lid)
        for p in info["participants"]:
            await http(port, "POST", "/api/lessons/%s/roles" % lid,
                       {"participant": p["id"], "service": "Поселение Щукино" if mode == "dds" else "Оператор 112"},
                       tokens[t], group="POST /roles")
    deadline = time.time() + a.duration
    runners = [asyncio.create_task(s.run(deadline)) for s, _ in students]
    operators = [s for s, _ in students if s.mode == "cards"][:a.voice] if clips else []
    speakers = [asyncio.create_task(Speaker(s, clips).run(deadline)) for s in operators]
    await asyncio.sleep(1.0)
    for lid, mode, t in lessons:
        await http(port, "POST", "/api/lessons/%s/start" % lid, token=tokens[t], group="POST /start")
    started = time.time()
    # фоном: экраны преподавателей опрашивают список занятий, администратор — мониторинг
    st, adm = await http(port, "POST", "/api/login", {"login": "admin", "password": PASSWORD}, group="POST /api/login")
    pollers = [asyncio.create_task(poll(port, tok, "/api/lessons", stop, 3.0, "GET /api/lessons")) for tok in tokens.values()]
    pollers.append(asyncio.create_task(poll(port, adm["token"], "/api/admin/status", stop, 5.0, "GET /api/admin/status")))
    # одновременность: в какой-то момент идут все сессии
    await asyncio.sleep(5)
    st, status = await http(port, "GET", "/api/admin/status", token=adm["token"], group="GET /api/admin/status")
    running_peak = status["lessons"]["running"]
    # 4) завершение: 112 — когда очереди кончились, ДДС — после последних докладов бригады
    await asyncio.sleep(max(0, started + a.lesson_s - time.time()))
    for lid, mode, t in lessons:
        await http(port, "POST", "/api/lessons/%s/finish" % lid, token=tokens[t], group="POST /finish")
    await asyncio.wait_for(asyncio.gather(*runners), timeout=a.duration + 30)
    await asyncio.wait_for(asyncio.gather(*speakers), timeout=30)
    st, status_end = await http(port, "GET", "/api/admin/status", token=adm["token"], group="GET /api/admin/status")
    stop.set()
    for p in pollers:
        p.cancel()
    # 5) отчёты и аналитика
    t_an = time.perf_counter()
    st, an = await http(port, "GET", "/api/analytics", token=tokens["t01"], group="GET /api/analytics")
    analytics_s = time.perf_counter() - t_an
    for lid, mode, t in lessons[:4]:
        await http(port, "GET", "/api/lessons/%s/report" % lid, token=tokens[t], group="GET /report")
        await http(port, "GET", "/api/lessons/%s/results" % lid, token=tokens[t], group="GET /results")
    return {"students": [s for s, _ in students], "lessons": lessons, "running_peak": running_peak,
            "login_all_s": round(login_s, 2), "analytics_s": round(analytics_s, 3),
            "analytics_server_ms": (an or {}).get("elapsed_ms"), "asr": (status_end or {}).get("asr"),
            "operators": len(operators)}


def db_write_bench(tmp: Path) -> float:
    """Запись в БД: журнал событий занятия, как пишет сервер (SQLite WAL), операций в секунду."""
    from store.db import Database
    from store.repos import Events, Lessons
    from store.schema import migrate
    db = Database("sqlite:///" + str(tmp / "bench.sqlite"))
    migrate(db)
    db.execute("INSERT INTO users (login, name, role, password_hash, created_at) VALUES ('b', 'b', 'teacher', 'x', 0)")
    lid = Lessons(db).create(1, "карточки", "p", "t", {}, "идёт")
    ev = Events(db)
    n = 3000
    t0 = time.perf_counter()
    for i in range(n):
        ev.add(lid, time.time(), "status", 1, {"card": str(i), "status": "Принята", "comment": "x" * 40})
    rate = n / (time.perf_counter() - t0)
    db.close()
    return rate


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=100)
    ap.add_argument("--sessions", type=int, default=20)
    ap.add_argument("--port", type=int, default=8130)
    ap.add_argument("--drops", type=int, default=10, help="сколько обучающихся рвут связь на 10–25 с")
    ap.add_argument("--lesson-s", dest="lesson_s", type=float, default=95.0, help="длительность занятий, с")
    ap.add_argument("--duration", type=float, default=150.0, help="предельное время клиентов, с")
    ap.add_argument("--keep", action="store_true", help="не удалять временную базу и журнал")
    ap.add_argument("--arrival", type=float, default=20.0, help="за сколько секунд входят обучающиеся")
    ap.add_argument("--cpus", type=int, default=0, help="ограничить сервер числом ядер (4 — как i5 демо-стенда)")
    ap.add_argument("--voice", type=int, default=20, help="сколько операторов 112 говорят в микрофон (0 — без голоса)")
    a = ap.parse_args()
    clips, voice_note = (None, "голос не проверялся (--voice 0)") if a.voice <= 0 else (None, "")
    if a.voice > 0:
        print("фразы оператора для микрофона (piper)…", flush=True)
        clips, voice_note = voice_clips()
        if clips is None:
            print("голос не проверяется:", voice_note, flush=True)
    tmp = Path(tempfile.mkdtemp(prefix="a112-load-"))
    port = free_port(a.port)
    db_url = "sqlite:///" + str(tmp / "load.sqlite")
    print("учётные записи: %d обучающихся, %d преподавателей…" % (a.users, max(1, a.sessions // 4)), flush=True)
    seed(db_url, a.users, max(1, a.sessions // 4))
    env = dict(os.environ, A112_DB=db_url, A112_VAR=str(tmp / "var"), A112_TTS="off", A112_TTS_DIR=str(tmp / "tts"),
               PYTHONIOENCODING="utf-8")
    server = subprocess.Popen([sys.executable, str(ROOT / "server" / "run.py"), "--port", str(port)], env=env,
                              stdout=open(tmp / "server.log", "w", encoding="utf-8"), stderr=subprocess.STDOUT)
    import psutil
    proc = psutil.Process(server.pid)
    samples = []
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.3)
        time.sleep(1.5)
        if a.cpus:  # эмуляция слабой машины: сервер (и дочерний процесс на Windows) — только на первых ядрах
            for p in [proc] + proc.children(recursive=True):
                p.cpu_affinity(list(range(min(a.cpus, psutil.cpu_count()))))

        async def sampled():
            async def sampler():
                # на Windows python.exe из venv — пускач, сервер работает в дочернем процессе: считаем всех
                procs = {}
                while True:
                    await asyncio.sleep(1)
                    try:
                        for p in [proc] + proc.children(recursive=True):
                            if p.pid not in procs:
                                procs[p.pid] = p
                                p.cpu_percent(None)
                        cpu = sum(p.cpu_percent(None) for p in procs.values() if p.is_running())
                        mem = sum(p.memory_info().rss for p in procs.values() if p.is_running())
                        samples.append({"cpu": cpu, "rss_mb": mem / 2 ** 20})
                    except psutil.Error:
                        continue
            task = asyncio.create_task(sampler())
            try:
                return await main_async(a, port, tmp, clips)
            finally:
                task.cancel()
        print("сессий %d, обучающихся %d, обрывов связи %d — идёт тест (~%d с)…" % (a.sessions, a.users, a.drops, a.lesson_s + 20), flush=True)
        t_all = time.perf_counter()
        res = asyncio.run(sampled())
        wall = time.perf_counter() - t_all
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
    # данные не потеряны: каждая отправленная команда подтверждена, итог получен каждым
    from store.db import Database
    db = Database(db_url)
    events = db.scalar("SELECT COUNT(*) AS n FROM lesson_events") or 0
    statuses = db.scalar("SELECT COUNT(*) AS n FROM lesson_events WHERE kind = 'status'") or 0
    saves = db.scalar("SELECT COUNT(*) AS n FROM lesson_events WHERE kind = 'save'") or 0
    results = db.scalar("SELECT COUNT(*) AS n FROM results") or 0
    span = db.one("SELECT MIN(at) AS a, MAX(at) AS b FROM lesson_events")
    db.close()
    studs = res["students"]
    lost = sum(len(s.pending) for s in studs)
    no_result = [s.login for s in studs if s.result is None]
    rate = db_write_bench(tmp)
    table = STATS.table()
    ui = [r for r in table if not r["group"].startswith(("WS", "голос")) and "залпом" not in r["group"]]
    worst_p95 = max(r["p95"] for r in ui)
    ws_p95 = max((r["p95"] for r in table if r["group"].startswith("WS") and "обрыва" not in r["group"]), default=0)
    errors5xx = sum(r["errors"] for r in table)
    cpu = [x["cpu"] for x in samples]
    rss = [x["rss_mb"] for x in samples]
    checks = [
        ("Отклик интерфейса при %d пользователях: 95%% запросов ≤ 2 с" % a.users, worst_p95 <= 2000, "худший p95 — %.0f мс" % worst_p95),
        ("Команды АРМ по WebSocket: подтверждение ≤ 2 с", ws_p95 <= 2000, "p95 — %.0f мс (без досланных после обрыва)" % ws_p95),
        ("Одновременных сессий ≥ 20", res["running_peak"] >= min(20, a.sessions), "шли одновременно: %d" % res["running_peak"]),
        ("Запись в БД ≥ 100 операций/с", rate >= 100, "%.0f операций/с (журнал событий, SQLite WAL)" % rate),
        ("Аналитический отчёт ≤ 30 с", res["analytics_s"] <= 30, "%.2f с (расчёт на сервере %s мс)" % (res["analytics_s"], res["analytics_server_ms"])),
        ("Сбой сети до 30 с: сессия восстановлена, данные не потеряны", lost == 0 and not no_result,
         "обрывов: %d; неподтверждённых команд: %d; без итога: %d" % (sum(s.reconnects for s in studs), lost, len(no_result))),
        ("Ошибок сервера (5xx)", errors5xx == 0, str(errors5xx)),
    ]
    voice_row = next((r for r in table if r["group"] == "голос: итог после конца речи"), None)
    if clips and res["operators"]:
        asr = res.get("asr") or {}
        heard = VOICE["finals"] / max(1, VOICE["phrases"])
        checks.append(("Голос: итог фразы ≤ 1 с после конца речи (%d операторов говорят в микрофон)" % VOICE["speakers"],
                       bool(voice_row) and voice_row["p95"] <= 1000 and VOICE["unavailable"] == 0 and heard >= 0.9,
                       "p95 — %.0f мс; распознано фраз %d из %d, вопрос определён верно в %d%% всех фраз; распознавание — "
                       "%.0f%% длительности речи на одном ядре"
                       % (voice_row["p95"] if voice_row else 0, VOICE["finals"], VOICE["phrases"],
                          100 * VOICE["correct"] / max(1, VOICE["phrases"]), 100 * (asr.get("load") or 0))))
    out_dir = ROOT / "build" / "loadtest"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    import platform
    host = {"cpu": platform.processor() or platform.machine(), "cores": psutil.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 2 ** 30),
            "os": platform.platform(), "python": platform.python_version()}
    data = {"when": stamp, "host": host, "users": a.users, "sessions": a.sessions, "wall_s": round(wall, 1),
            "login_all_s": res["login_all_s"], "events": events, "statuses": statuses, "saves": saves, "results": results,
            "events_per_s_observed": round(events / max(1.0, (span["b"] or 0) - (span["a"] or 0)), 1) if span and span["a"] else None,
            "db_write_rate": round(rate), "cpu_peak": max(cpu, default=0), "cpu_avg": round(sum(cpu) / len(cpu), 1) if cpu else 0,
            "rss_peak_mb": round(max(rss, default=0)), "table": table, "checks": [{"check": c, "ok": ok, "value": v} for c, ok, v in checks],
            "notes": STATS.notes[:20], "voice": dict(VOICE, note=voice_note, asr=res.get("asr"))}
    (out_dir / ("report-%s.json" % stamp)).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    md = ["# Нагрузочный тест %s" % stamp, "",
          "Машина: %s, %d логических ядер, %d ГБ ОЗУ, %s, Python %s.%s" % (host["cpu"], host["cores"], host["ram_gb"], host["os"],
                                                                          host["python"], " Сервер ограничен %d ядрами." % a.cpus if a.cpus else ""),
          "Нагрузка: %d обучающихся в %d одновременных сессиях (половина — ДДС с ускорением ×10, половина — оператор 112), "
          "5 экранов преподавателя и мониторинг администратора; %d обучающихся рвут связь на 10–25 с.%s" % (
              a.users, a.sessions, a.drops, " %d операторов 112 задают вопросы голосом: фразы оператора (голоса piper) в "
              "реальном времени через микрофонный канал, паузы 2–6 с на ответ заявителя и карточку." % VOICE["speakers"]
              if clips and res["operators"] else (" Голос: " + voice_note if voice_note else "")),
          "", "| Требование ТЗ | Итог | Значение |", "|---|---|---|"]
    md += ["| %s | %s | %s |" % (c, "выполнено" if ok else "НЕ выполнено", v) for c, ok, v in checks]
    md += ["", "Вход залпом (все %d в одну секунду, для сведения): %.1f с на всех — пароль проверяется PBKDF2 в 200 000 итераций, "
           "это защита от перебора; при входе в течение %d с отклик — в таблице (POST /api/login). "
           "Событий занятий записано: %d (статусов %d, карточек %d), "
           "результатов: %d. Сервер: процессор до %.0f%% одного ядра (в среднем %.0f%%), память до %d МБ." %
           (a.users, res["login_all_s"], a.arrival, events, statuses, saves, results, data["cpu_peak"], data["cpu_avg"], data["rss_peak_mb"]),
           "", "| Запрос | Число | p50, мс | p95, мс | max, мс | ошибок |", "|---|---|---|---|---|---|"]
    md += ["| %s | %d | %s | %s | %s | %d |" % (r["group"], r["n"], r["p50"], r["p95"], r["max"], r["errors"]) for r in table]
    (out_dir / ("report-%s.md" % stamp)).write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print("\nотчёт: %s" % (out_dir / ("report-%s.md" % stamp)))
    if not a.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print("временные данные: %s" % tmp)
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
