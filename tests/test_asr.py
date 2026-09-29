"""Распознавание речи оператора 112 (узел 9): словарь, вопросы по распознанному тексту, поток распознавания,
микрофон по WebSocket и вопрос заявителю голосом."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from arm112.paths import data_dir
from domain.calls import detect_intent
from speech import asr as asr_mod
from speech.asr import Asr, AsrUnavailable, clean, vocabulary
from test_api import hello, login, make_cards_world, recv

VOCABULARY = data_dir() / "asr" / "vocabulary.txt"
MODEL_DIR = data_dir() / "asr" / asr_mod.MODEL


# ---------- вопрос по распознанному тексту ----------
@pytest.mark.parametrize("text,intent", [
    ("кем вы приходите с пострадавшему", "status"),        # «приходитесь» распознано как «приходите с»
    ("Кем вы приходитесь пострадавшему?", "status"),       # вопрос о статусе, хоть и есть «пострадавш»
    ("пострадавший приходит в сознание", "victims"),
    ("то пострадал", "victims"),                              # «кто пострадал?»: «кто» потеряно
    ("служба сто двенадцать слушай вас то случилось", "details"),   # «что» потеряно, «слушаю» → «слушай»
    ("назовите адрес", "address"), ("уточните номер дома", "address_clarified"), ("как вас зовут", "name"),
    ("есть угроза людям", "danger"), ("запах газа чувствуете", "gas"), ("оставьте телефон для связи", "phone"),
    ("хорошо оставайтесь на линии", None)])
def test_intents_survive_recognition_errors(text, intent):
    assert detect_intent(text) == intent


def test_vocabulary_expands_word_forms():
    pytest.importorskip("pymorphy3")
    words = set(vocabulary(VOCABULARY))
    for w in ("зовут", "адреса", "пострадавшие", "приходитесь", "подъезде", "газа", "перезвоните", "случилось"):
        assert w in words, w
    assert len(words) > 2000


def test_clean_drops_unknown_word_marks():
    assert clean("[unk] назовите  [unk] адрес [unk]") == "назовите адрес"


def test_end_of_phrase_by_pause():
    """Конец фразы — 0,5 с тишины после речи; порог речи — над фоновым шумом, щелчок — не речь."""
    import math
    from array import array
    from speech.asr import END_SILENCE_S, Stream

    class Rec:                       # распознаватель, который сам конца фразы не находит
        def AcceptWaveform(self, pcm):
            return False

        def PartialResult(self):
            return '{"partial": ""}'

        def FinalResult(self):
            return '{"text": "назовите адрес"}'

        def Reset(self):
            pass

    def chunk(amplitude, noise=0):   # 0,1 с: тон 300 Гц и «шум» заданной громкости
        return array("h", [int(amplitude * math.sin(2 * math.pi * 300 * i / 16000) + (noise if i % 2 else -noise))
                           for i in range(1600)]).tobytes()

    class Owner:
        streams, utterances, busy_s, audio_s = 1, 0, 0.0, 0.0

    s = Stream(Owner(), Rec())
    for background in (0, 150):      # тишина цифровая и комнатный шум
        outs = [s._feed(chunk(0, background)) for _ in range(5)]
        outs += [s._feed(chunk(6000, background)) for _ in range(8)]              # 0,8 с речи
        quiet = [s._feed(chunk(0, background)) for _ in range(int(END_SILENCE_S * 10) + 1)]
        finals = [o for o in outs + quiet if o and o["t"] == "final"]
        assert len(finals) == 1 and quiet[int(END_SILENCE_S * 10) - 1] == finals[0], background
    assert s._feed(chunk(6000)) is None and [s._feed(chunk(0)) for _ in range(8)] == [None] * 8   # щелчок 0,1 с
    # длинная громкая фраза с тихим концом: конец фразы — после тихих слов, а не на них
    outs = [s._feed(chunk(6000)) for _ in range(30)] + [s._feed(chunk(1200)) for _ in range(3)]
    assert not [o for o in outs if o and o["t"] == "final"]
    quiet = [s._feed(chunk(0)) for _ in range(int(END_SILENCE_S * 10))]
    assert quiet[-1] and quiet[-1]["t"] == "final"
    # после тишины включился ровный гул (вентилятор) громче порога: через 8 с он — фон, а не речь,
    # и пауза в речи на его фоне заканчивает фразу (без этого правила фраза не закончилась бы никогда)
    s2 = Stream(Owner(), Rec())
    hum = [s2._feed(chunk(0)) for _ in range(5)] + [s2._feed(chunk(0, 400)) for _ in range(90)]
    speech = [s2._feed(chunk(6000, 400)) for _ in range(8)]
    tail = [s2._feed(chunk(0, 400)) for _ in range(int(END_SILENCE_S * 10))]
    finals = [o for o in hum + speech + tail if o and o["t"] == "final"]
    assert len(finals) == 1 and tail[-1] == finals[0]


# ---------- микрофон по WebSocket (распознавание подменено) ----------
class FakeStream:
    def __init__(self, owner):
        self.owner = owner
        self.bytes = 0

    async def feed(self, pcm):
        self.bytes += len(pcm)
        if self.bytes >= 3200 and self.owner.phrases:   # «фраза» — 0,1 с звука
            self.bytes = 0
            return {"t": "final", "text": self.owner.phrases.pop(0)}
        return {"t": "partial", "text": "…"} if len(pcm) == 320 else None

    async def end(self):
        return {"t": "final", "text": self.owner.phrases.pop(0)} if self.owner.phrases else None

    def close(self):
        self.owner.streams -= 1
        self.owner.closed += 1


class FakeAsr:
    def __init__(self, phrases=(), max_streams=5):
        self.phrases = list(phrases)
        self.max_streams = max_streams
        self.enabled = True
        self.streams = 0
        self.closed = 0

    async def open(self):
        if not self.enabled:
            raise AsrUnavailable("распознавание речи выключено администратором")
        if self.streams >= self.max_streams:
            raise AsrUnavailable("все каналы распознавания заняты — задавайте вопросы кнопками")
        self.streams += 1
        return FakeStream(self)

    async def ready(self):
        return True

    def status(self):
        return {"installed": True, "enabled": self.enabled, "loaded": True, "streams": self.streams,
                "max_streams": self.max_streams, "utterances": 0, "audio_s": 0.0, "load": None, "words": 100,
                "threads": 1, "error": None}

    def close(self):
        pass


def cards_lesson(client, classifier_engine):
    """Занятие «карточки» с одним обучающимся; вызов принят. Возвращает (L, заголовки, токен, pid)."""
    engine, clock, calls = classifier_engine
    th, _, _ = login(client, "t1")
    sh, stok, sid = login(client, "s1")
    L = client.post("/api/lessons", json={"pack_id": calls.id, "count": 1, "norm_s": 120}, headers=th).json()["id"]
    client.post("/api/lessons/%s/join" % L, headers=sh)
    client.post("/api/lessons/%s/roles" % L, json={"participant": sid, "service": "Оператор 112"}, headers=th)
    return L, th, stok, sid


def test_voice_question_reaches_the_caller(classifier, tmp_path):
    engine, clock, calls, db = make_cards_world(classifier, tmp_path)
    fake = FakeAsr(["назовите адрес", "хорошо оставайтесь на линии"])
    with TestClient(create_app(engine, db, tick_s=0.01, asr=fake)) as client:
        L, th, stok, sid = cards_lesson(client, (engine, clock, calls))
        with client.websocket_connect("/ws?lesson=" + L) as ws:
            hello(ws, stok)
            recv(ws, "state")
            client.post("/api/lessons/%s/start" % L, headers=th)
            clock.now += 3
            recv(ws, "incoming")
            ws.send_json({"t": "answer"})
            recv(ws, "caller_say")
            with client.websocket_connect("/ws/voice?lesson=" + L) as mic:
                hello(mic, stok)
                assert mic.receive_json()["t"] == "ready"
                mic.send_bytes(b"\0" * 320)                         # 10 мс звука — промежуточный текст
                assert mic.receive_json() == {"t": "partial", "text": "…"}
                mic.send_bytes(b"\0" * 3200)
                heard = mic.receive_json()
                assert heard == {"t": "final", "text": "назовите адрес", "intent": "address"}
                # страница отправляет распознанный вопрос тем же сообщением, что и кнопка
                ws.send_json({"t": "ask", "intent": heard["intent"], "text": heard["text"], "via": "voice"})
                assert recv(ws, "caller_say")["line"]["who"] == "заявитель"
                mic.send_json({"t": "end"})                          # кнопка «стоп»: итог сказанного сразу
                said = mic.receive_json()
                assert said["text"] == "хорошо оставайтесь на линии" and said["intent"] is None
                ws.send_json({"t": "say", "text": said["text"]})     # реплика без вопроса — заявитель молчит
            state = engine.get(L).desk.current[sid]
            lines = [(x["who"], x["text"], x.get("via")) for x in state.transcript]
            assert ("оператор", "назовите адрес", "voice") in lines
            assert lines[-1] == ("оператор", "хорошо оставайтесь на линии", "voice")
        assert fake.streams == 0 and fake.closed == 1                # канал распознавания освобождён
        rows = db.query("SELECT kind, data FROM lesson_events WHERE lesson_id = ?", (int(L[1:]),))
        kinds = [r["kind"] for r in rows]
        assert "say" in kinds and any('"voice"' in (r["data"] or "") for r in rows if r["kind"] == "ask")


def test_voice_socket_rejects_strangers_and_reports_unavailable(classifier, tmp_path):
    engine, clock, calls, db = make_cards_world(classifier, tmp_path)
    fake = FakeAsr(max_streams=1)
    with TestClient(create_app(engine, db, tick_s=0.01, asr=fake)) as client:
        L, th, stok, sid = cards_lesson(client, (engine, clock, calls))
        _, ttok, _ = login(client, "t1")
        _, s2tok, _ = login(client, "s2")
        for token in ("нет такого", ttok, s2tok):                   # без входа, преподаватель, не участник
            with client.websocket_connect("/ws/voice?lesson=" + L) as mic:
                hello(mic, token)
                with pytest.raises(WebSocketDisconnect) as exc:
                    mic.receive_json()
                assert exc.value.code in (4401, 4403)
        with client.websocket_connect("/ws/voice?lesson=" + L) as first:
            hello(first, stok)
            assert first.receive_json()["t"] == "ready"
            with client.websocket_connect("/ws/voice?lesson=" + L) as second:   # каналов больше нет
                hello(second, stok)
                busy = second.receive_json()
                assert busy["t"] == "unavailable" and "заняты" in busy["reason"]
            first.send_bytes(b"\0" * (70 * 1024))                  # кусок больше 2 с — отбрасывается
            first.send_json({"t": "end"})                          # фраз нет — итога нет, сокет жив
        fake.enabled = False
        with client.websocket_connect("/ws/voice?lesson=" + L) as mic:
            hello(mic, stok)
            off = mic.receive_json()
            assert off["t"] == "unavailable" and "выключено" in off["reason"]


def test_admin_sees_speech_recognition(classifier, tmp_path):
    engine, clock, calls, db = make_cards_world(classifier, tmp_path)
    with TestClient(create_app(engine, db, tick_s=0.01, asr=FakeAsr())) as client:
        ah, _, _ = login(client, "admin")
        status = client.get("/api/admin/status", headers=ah).json()
        assert status["asr"]["installed"] is True
        names = {c["name"]: c for c in status["components"]}
        assert names["Распознавание речи"]["status"] == "ok"
        services = {s["id"]: s for s in client.get("/api/admin/services", headers=ah).json()}
        assert services["asr"]["on"] is True
        assert client.get("/api/health").json()["asr"]["installed"] is True   # проверка установки без входа


# ---------- настоящее распознавание (Vosk и модель; фразы оператора озвучивает piper) ----------
PHRASES = [("Назовите адрес, пожалуйста.", "address"), ("Как вас зовут?", "name"),
           ("Есть пострадавшие?", "victims"), ("Кем вы приходитесь пострадавшему?", "status"),
           ("Уточните номер дома и подъезд.", "address_clarified"), ("Есть угроза людям?", "danger"),
           ("Запах газа чувствуете?", "gas"), ("Как проехать к вашему дому?", "access"),
           ("Оставьте номер телефона для связи.", "phone"), ("Расскажите подробнее, что произошло.", "details")]


@pytest.fixture(scope="module")
def spoken():
    """Фразы оператора: 16 кГц, 16 бит — как с микрофона."""
    pytest.importorskip("vosk")
    pytest.importorskip("piper")
    if not (MODEL_DIR / "am" / "final.mdl").is_file():
        pytest.skip("нет модели распознавания: python tools/fetch_asr.py")
    voices = data_dir() / "voices"
    if not (voices / "ru_RU-dmitri-medium.onnx").is_file():
        pytest.skip("нет голосов piper: python tools/fetch_voices.py")
    import numpy as np
    from speech.dsp import resample
    from speech.synth import PiperSynth
    synth = PiperSynth(voices, threads=2)
    out = []
    for text, intent in PHRASES:
        audio, rate = synth.synthesize(text, "ru_RU-dmitri-medium", noise=0.0)  # без случайной вариации — тест стабилен
        y = np.clip(resample(audio, rate, asr_mod.RATE) * 32767, -32768, 32767).astype("<i2").tobytes()
        out.append((text, intent, b"\0" * 8000 + y + b"\0" * 19200))  # пауза до и после (0,6 с) — как в разговоре
    return out


def test_vosk_recognizes_operator_questions(spoken):
    """Каждая фраза заканчивается сама — паузой после речи, без команды «end»."""
    async def run():
        asr = Asr(MODEL_DIR, VOCABULARY, threads=2)
        stream = await asr.open()
        results = []
        for text, intent, pcm in spoken:
            finals = []
            for i in range(0, len(pcm), 3200):                   # куски по 0,1 с — как шлёт браузер
                out = await stream.feed(pcm[i:i + 3200])
                if out and out["t"] == "final":
                    finals.append(out["text"])
            assert await stream.end() is None, text              # после паузы досказывать нечего
            results.append((text, " ".join(finals), intent))
        stream.close()
        status = asr.status()
        asr.close()
        return results, status

    results, status = asyncio.run(run())
    good = [r for r in results if detect_intent(r[1]) == r[2]]
    assert len(good) >= len(results) - 1, results                  # не больше одной ошибки из десяти
    assert status["streams"] == 0 and status["utterances"] >= len(results) - 1
    assert status["load"] < 0.5                                     # распознавание быстрее речи


def test_microphone_to_caller_with_real_recognizer(classifier, tmp_path, spoken):
    engine, clock, calls, db = make_cards_world(classifier, tmp_path)
    asr = Asr(MODEL_DIR, VOCABULARY, threads=2)
    text, intent, pcm = spoken[0]
    with TestClient(create_app(engine, db, tick_s=0.01, asr=asr)) as client:
        L, th, stok, sid = cards_lesson(client, (engine, clock, calls))
        client.post("/api/lessons/%s/start" % L, headers=th)
        with client.websocket_connect("/ws/voice?lesson=" + L) as mic:
            hello(mic, stok)
            assert mic.receive_json()["t"] == "ready"
            for i in range(0, len(pcm), 3200):
                mic.send_bytes(pcm[i:i + 3200])
            mic.send_json({"t": "end"})
            while True:
                msg = mic.receive_json()
                if msg["t"] == "final":
                    break
        assert msg["intent"] == intent, msg
    asr.close()
