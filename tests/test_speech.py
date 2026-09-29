"""Озвучка (узел 5): текст для синтезатора, голоса, кэш звука, телефонная линия, ограничение ресурсов,
фоновая очередь, звук в репликах заявителя и докладах бригады, раздача звука сервером."""
import importlib.util
import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from arm112.paths import data_dir
from speech import catalog as cat
from speech.catalog import SpeechCatalog, caller_voice, pack_phrases, speakable
from speech.dsp import ring_wav, write_wav
from speech.queue import TtsQueue

SERVER = Path(__file__).resolve().parents[1] / "server"
TESTS = Path(__file__).resolve().parent
VOICES = data_dir() / "voices"


def wav_info(path):
    with wave.open(str(path), "rb") as w:
        return w.getframerate(), w.getnchannels(), w.getnframes()


# ---------- текст для синтезатора ----------
def test_speakable_reads_like_a_person():
    assert speakable("Мой номер — +79161263471.") == \
        "Мой номер, плюс семь, девятьсот шестнадцать, сто двадцать шесть, тридцать четыре, семьдесят один."
    assert speakable("звоните 916-126-34-71") == "звоните девятьсот шестнадцать, сто двадцать шесть, тридцать четыре, семьдесят один"
    assert speakable("8 (916) 000-05-07") == "восемь, девятьсот шестнадцать, ноль ноль ноль, ноль пять, ноль семь"
    assert speakable("Горит квартира на 12 этаже") == "Горит квартира на двенадцатом этаже"
    assert speakable("на 23 этаже") == "на двадцать третьем этаже"
    assert speakable("ДТП на ул. Рогова, д. 5, кв. 7") == "дэ тэ пэ на улице Рогова, дом 5, квартира 7"
    assert speakable("около ст. Киевская, 1 км. 2 стр.2") == "около станции Киевская, 1 километр 2 строение 2"
    assert speakable("карточка 36907101, дом 112") == "карточка 36907101, дом 112"   # не телефон — не трогаем
    assert speakable("УК «Уютный дом»") == "у ка Уютный дом"


def test_number_words():
    assert [cat.number_words(n) for n in (0, 7, 11, 40, 99, 100, 305, 999)] == \
        ["ноль", "семь", "одиннадцать", "сорок", "девяносто девять", "сто", "триста пять", "девятьсот девяносто девять"]


# ---------- голоса и фразы ----------
def test_caller_voice_follows_gender_and_is_stable():
    f = {"key": "g01", "caller": {"name": "Иванова Ольга", "gender": "f"}}
    m = {"key": "g02", "caller": {"name": "Петров Олег", "gender": "m"}}
    assert caller_voice(f).voice in cat.FEMALE and caller_voice(m).voice in cat.MALE
    assert caller_voice(f) == caller_voice(dict(f))
    scales = {caller_voice({"key": "k%d" % i, "caller": {"name": "x"}}).scale for i in range(60)}
    assert min(scales) >= 0.92 and max(scales) <= 1.08 and len(scales) > 5       # заявители говорят в разном темпе
    calm, panic = caller_voice(m), caller_voice(dict(m, gen={"difficulty": 3}))
    assert panic.scale == round(calm.scale - 0.06, 2)                             # взволнованный — быстрее


def test_pack_phrases_cover_every_voiced_line():
    calls = json.loads((data_dir() / "scenarios" / "calls_bilety.json").read_text(encoding="utf-8"))
    phrases = pack_phrases(calls)
    keys = {p["key"] for p in phrases}
    assert len(keys) == len(phrases) > 500
    for call in calls["calls"][:20]:
        spec = caller_voice(call)
        for text in call["dialogue"].values():
            assert cat.key(text, spec) in keys
        assert cat.key(cat.UNKNOWN, spec) in keys
    dds = json.loads((data_dir() / "scenarios" / "dds_shchukino_v0.json").read_text(encoding="utf-8"))
    brigade = pack_phrases(dds)
    texts = {r["text"] for c in dds["cards"] for r in c.get("brigade", [])}
    assert {p["text"] for p in brigade} == texts and {p["voice"] for p in brigade} == {cat.BRIGADE.voice}


def test_unknown_phrase_matches_the_caller_bot():
    from domain.calls import Caller
    assert cat.UNKNOWN == Caller.UNKNOWN


def test_catalog_finds_audio_by_text(tmp_path):
    c = SpeechCatalog(tmp_path)
    call = {"key": "g01", "caller": {"name": "Иванова Ольга", "gender": "f"}}
    assert c.caller_audio(call, "Пострадавших нет.") is None
    k = cat.key("Пострадавших нет.", caller_voice(call))
    write_wav(c.path(k), b"\0\0" * 80, 8000)
    assert c.caller_audio(call, "Пострадавших нет.") == "/tts/%s.wav" % k
    assert c.brigade_audio("Пострадавших нет.") is None                            # другой голос — другой файл
    assert c.missing([{"key": k}, {"key": "x" * 32}]) == [{"key": "x" * 32}]


# ---------- звук ----------
def test_ring_is_a_valid_phone_wav(tmp_path):
    ring_wav(tmp_path / "ring.wav")
    rate, channels, frames = wav_info(tmp_path / "ring.wav")
    assert (rate, channels) == (8000, 1) and frames == 16000
    c = SpeechCatalog(tmp_path / "c")
    assert c.ensure_ring() == "/tts/ring.wav" and (tmp_path / "c" / "ring.wav").is_file()


def test_phone_line_is_band_limited_8khz():
    np = pytest.importorskip("numpy")
    from speech.dsp import alaw_roundtrip, phone_line, resample
    rate = 22050
    t = np.arange(rate) / rate
    x = 0.3 * np.sin(2 * np.pi * 100 * t) + 0.3 * np.sin(2 * np.pi * 1000 * t) + 0.3 * np.sin(2 * np.pi * 6000 * t)
    y = phone_line(x, rate)
    assert y.dtype == np.dtype("<i2") and len(y) == 8000
    assert abs(int(np.max(np.abs(y))) - int(0.89 * 32767)) < 400                  # уровень −1 дБ
    spec = np.abs(np.fft.rfft(y.astype(float)))
    freq = np.fft.rfftfreq(len(y), 1 / 8000)
    band = spec[(freq > 900) & (freq < 1100)].max()
    assert spec[(freq > 80) & (freq < 120)].max() < band * 0.05                  # 100 Гц срезаны
    z = np.linspace(-1, 1, 1001)
    assert np.max(np.abs(alaw_roundtrip(z) - z)) < 0.03                           # A-закон: грубее, но разборчиво
    assert len(resample(np.zeros(8000), 8000, 22050)) == 22050


# ---------- ограничение ресурсов процесса озвучки ----------
@pytest.mark.skipif(not hasattr(os, "sched_setaffinity") and os.name != "nt", reason="нет привязки к ядрам")
def test_limits_hold_cpu_priority_and_memory():
    psutil = pytest.importorskip("psutil")
    code = (
        "import sys, json\nsys.path.insert(0, %r)\nfrom speech import limits\n"
        "applied = limits.apply(2, 200, True)\nimport psutil\np = psutil.Process()\n"
        "res = {'applied': applied, 'affinity': len(p.cpu_affinity()), 'nice': p.nice()}\n"
        "try:\n    x = bytearray(400 * 2 ** 20)\n    res['alloc'] = 'ok'\nexcept MemoryError:\n    res['alloc'] = 'memory'\n"
        "print(json.dumps(res))\n" % str(SERVER))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["affinity"] == min(2, psutil.cpu_count()) and res["alloc"] == "memory"
    assert res["nice"] == (psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
    assert res["applied"]["memory_mb"] == 200


# ---------- очередь озвучки ----------
def fake_voices(path):
    path.mkdir(parents=True, exist_ok=True)
    for v in cat.MALE + cat.FEMALE:
        (path / (v + ".onnx")).write_bytes(b"")
        (path / (v + ".onnx.json")).write_text("{}", encoding="utf-8")
    return path


@pytest.mark.skipif(importlib.util.find_spec("piper") is None, reason="нет piper")
def test_queue_renders_missing_phrases_in_background(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(TESTS))
    c = SpeechCatalog(tmp_path / "tts")
    q = TtsQueue(c, fake_voices(tmp_path / "voices"), module="fake_tts_render")
    phrases = [{"key": cat.key(t, cat.BRIGADE), "text": t, "voice": cat.BRIGADE.voice, "scale": 1.0}
               for t in ("Бригада на месте.", "Работы завершены.", "сбой синтеза")]
    phrases.append({"key": "n" * 32, "text": "нет такого голоса", "voice": "ru_RU-nobody", "scale": 1.0})
    assert q.enqueue(phrases) == 3                                               # голоса без модели не ставим
    q.wait(30)
    st = q.status()
    assert st["rendered"] == 2 and st["failed"] == 1 and st["pending"] == 0 and not st["running"]
    assert "тест" in st["last_error"] and st["last"]["rendered"] == 2
    assert c.brigade_audio("Бригада на месте.") and c.brigade_audio("сбой синтеза") is None
    assert q.enqueue(phrases[:2]) == 0                                           # уже озвучено — второй раз не делаем
    off = TtsQueue(c, tmp_path / "voices", autostart=False)
    assert off.enqueue(phrases[2:3]) == 1 and not off.running() and off.status()["pending"] == 1


@pytest.mark.skipif(importlib.util.find_spec("piper") is None, reason="нет piper")
def test_queue_survives_a_crashed_render_process(tmp_path, monkeypatch):
    """Процесс озвучки упал на фразе — она сбойная, остальные озвучивает новый процесс."""
    monkeypatch.setenv("PYTHONPATH", str(TESTS))
    c = SpeechCatalog(tmp_path / "tts")
    q = TtsQueue(c, fake_voices(tmp_path / "voices"), module="fake_tts_render")
    texts = ["Первая фраза.", "авария синтеза", "Третья фраза.", "Четвёртая фраза."]
    phrases = [{"key": cat.key(t, cat.BRIGADE), "text": t, "voice": cat.BRIGADE.voice, "scale": 1.0} for t in texts]
    q.enqueue(phrases)
    q.wait(60)
    st = q.status()
    assert st["rendered"] == 3 and st["failed"] == 1 and st["pending"] == 0
    assert "аварийно" in st["last_error"] and "bad allocation" in st["last_error"]
    assert c.brigade_audio("Четвёртая фраза.") and c.brigade_audio("авария синтеза") is None


def test_long_phrases_are_split_for_synthesis():
    long = ("У подъезда молодой человек, лет 20, в синей джинсовой куртке в синих джинсах, черных ботинках, славянской "
            "внешности, стоит у домофона и кого-то ждет, в подъезд не заходит и причину своего присутствия старшей по "
            "подъезду не пояснил; Иванова Инна Васильевна (старшая по подъезду), 916 896 3254")
    parts = cat.split_for_synthesis(long)
    assert len(parts) >= 3 and all(len(p) <= 110 for p, _ in parts)
    assert " ".join(p for p, _ in parts).split() == long.split()                  # ничего не потеряно
    assert parts[-1][1] == 0.3 and all(pause == 0.12 for _, pause in parts[:-1])
    assert cat.split_for_synthesis("Алло! Помогите.") == [("Алло!", 0.3), ("Помогите.", 0.3)]
    word = "а" * 150                                                              # без пробелов — режем по границе
    assert [len(p) for p, _ in cat.split_for_synthesis(word)] == [110, 40]


@pytest.mark.skipif(importlib.util.find_spec("piper") is None or not (VOICES / "ru_RU-ruslan-medium.onnx").is_file(),
                    reason="нет piper или голосов в data/voices")
def test_real_piper_render_under_background_limits(tmp_path):
    jobs = [{"key": cat.key(t, cat.BRIGADE), "text": t, "voice": cat.BRIGADE.voice, "scale": 1.0}
            for t in ("Бригада на месте, перекрываем стояк.", "Работы завершены.")]
    (tmp_path / "jobs.json").write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
    out = subprocess.run([sys.executable, "-m", "speech.render", "--jobs", str(tmp_path / "jobs.json"), "--out",
                          str(tmp_path / "tts"), "--voices", str(VOICES), "--profile", "background"],
                         cwd=str(SERVER), capture_output=True, text=True, timeout=240,
                         env=dict(os.environ, PYTHONPATH=str(SERVER), PYTHONIOENCODING="utf-8"))
    events = [json.loads(line) for line in out.stdout.splitlines() if line.startswith("{")]
    assert events[0]["limits"]["cpus"] == 1 and events[0]["threads"] == 1, out.stdout + out.stderr
    finish = events[-1]
    assert finish["event"] == "finish" and finish["rendered"] == 2 and finish["peak_mb"] < 1024
    for j in jobs:
        rate, channels, frames = wav_info(tmp_path / "tts" / (j["key"] + ".wav"))
        assert (rate, channels) == (8000, 1) and frames > 4000                    # больше полсекунды речи


# ---------- звук в занятии ----------
def test_caller_lines_and_brigade_reports_carry_audio(classifier, tmp_path):
    from domain import generator as gen
    from domain.calls import CallPack
    from domain.lesson import Actor, ActorKind
    from test_api import make_engine
    engine, clock, dds = make_engine(classifier)
    c = SpeechCatalog(tmp_path)
    engine.speech = c
    raw = gen.generate_calls(gen.Gazetteer.load(), ["14"], 1, 1, seed=5)
    calls = CallPack(raw)
    engine.packs[calls.id] = calls
    call = calls.calls[0]
    k = cat.key(call["dialogue"]["opening"], caller_voice(call))
    write_wav(c.path(k), b"\0\0" * 80, 8000)
    teacher = Actor("t1", ActorKind.TEACHER)
    rt = engine.create_lesson(teacher, calls.id, config={"count": 1})
    engine.join(rt.lesson.id, "s1", "S1")
    engine.assign_role(teacher, rt.lesson.id, "s1", "Оператор 112")
    engine.start(teacher, rt.lesson.id)
    clock.now += 5
    engine.tick()
    said = [m for _, m in engine.handle(rt.lesson.id, "s1", {"t": "answer"}) if m["t"] == "caller_say"]
    assert said[0]["line"]["audio"] == "/tts/%s.wav" % k
    asked = [m for _, m in engine.handle(rt.lesson.id, "s1", {"t": "ask", "intent": "name"}) if m["t"] == "caller_say"]
    assert "audio" not in asked[0]["line"]                                       # не озвучено — только текст

    first = dds.cards[0]
    rep = first["brigade"][0]
    write_wav(c.path(cat.key(rep["text"], cat.BRIGADE)), b"\0\0" * 80, 8000)
    lesson = engine.create_lesson(teacher, dds.id)
    engine.join(lesson.lesson.id, "s2", "S2")
    engine.assign_role(teacher, lesson.lesson.id, "s2", dds.role)
    engine.start(teacher, lesson.lesson.id)
    clock.now += first["at"]
    engine.tick()
    engine.handle(lesson.lesson.id, "s2", {"t": "set_status", "card": lesson.numbers[first["key"]], "status": "Принята",
                                          "comment": "принята", "seq": 1})
    clock.now += rep["after"] + 1
    reports = [m for to, m in engine.tick() if to == "s2" and m["t"] == "call"]
    assert reports[0]["text"] == rep["text"] and reports[0]["audio"].startswith("/tts/")


def test_server_serves_audio_and_reports_tts(classifier, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from test_api import login, make_db, make_engine
    engine, _, _ = make_engine(classifier)
    db, _ = make_db(tmp_path / "tts.sqlite")
    app = create_app(engine, db, tick_s=0.01, tts_dir=tmp_path / "tts", voices_dir=fake_voices(tmp_path / "voices"),
                     tts_autostart=False)
    with TestClient(app) as client:
        res = client.get("/tts/ring.wav")
        assert res.status_code == 200 and res.headers["content-type"].startswith("audio/")
        assert "immutable" in res.headers["cache-control"]
        tts = client.get("/api/health").json()["tts"]
        assert tts["voices"] == sorted(cat.MALE + cat.FEMALE) and tts["pending"] == 16 and not tts["running"]
        th, _, _ = login(client, "t1")
        sc = client.post("/api/scenarios/generate", json={"mode": "card_actions", "district": "Щукино", "count": 4,
                                                          "seed": 1}, headers=th).json()
        client.post("/api/scenarios/%d/approve" % sc["id"], json={}, headers=th)
        assert client.get("/api/health").json()["tts"]["pending"] > 16                # утверждённые доклады — в очередь
        assert engine.speech is not None
