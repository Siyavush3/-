"""Автопилот ДДС: бот-диспетчер проходит занятие строго по эталону, а человек смотрит (узел 8).

    python tools/demo_dds.py [--server http://127.0.0.1:8112] [--pack dds-shchukino-v0] [--tempo 0.5]
                             [--teacher teacher] [--student student2] [--think 6]

Работа ДДС быстрая: карточки идут каждую минуту, первичный статус — за 30 секунд. Чтобы понять, как
выглядит правильная работа, запустите автопилот и откройте АРМ той же учётной записью обучающегося
(по умолчанию student2) — в браузере видно, как приходят карточки, как бот ставит «Принята» или
«Не принята» с комментарием, как после докладов бригады появляются статусы хода работ. В конце —
итог и ссылка на отчёт о занятии. Эталон бот читает из файла сценария (data/scenarios) — поэтому
запускается на машине сервера. Пароли демо-учёток — data/demo/accounts.json.
"""
import argparse
import asyncio
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]


def call(base, method, path, body=None, token=None):
    req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def comment_for(expect: dict, key: str, numbers: dict) -> str:
    """Комментарий по эталону: образец из сценария или сборка из обязательных фактов."""
    text = expect.get(key) or ""
    if not text:
        facts = expect.get("comment_facts" if key == "sample_comment" else "final_comment_facts") or []
        text = "; ".join(g[0] for g in facts)
    return re.sub(r"\{(\w+)\}", lambda m: numbers.get(m.group(1), m.group(1)), text)


async def bot(base, token, lesson, pack, think, finish_at, log):
    by_key = {c["key"]: c for c in pack["cards"]}
    numbers = {}           # ключ карточки → номер в занятии (для ссылок «дубль карточки …»)
    seq = 0
    ws_url = base.replace("http", "ws", 1) + "/ws?lesson=" + lesson
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"t": "hello", "token": token}))

        async def status(card, st, comment, delay):
            nonlocal seq
            await asyncio.sleep(delay)
            seq += 1
            await ws.send(json.dumps({"t": "set_status", "card": card, "status": st, "comment": comment, "seq": seq}))
            log("  %s: «%s»%s" % (card, st, " — " + comment if comment else ""))

        while time.time() < finish_at:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
            except asyncio.TimeoutError:
                continue
            if msg["t"] == "card":
                c = msg["card"]
                spec = next((s for s in pack["cards"] if s["description"] == c.get("description")), None)
                if spec is None:
                    continue
                numbers[spec["key"]] = c["number"]
                exp = spec["expect"]
                log("карточка %s (%s): %s" % (c["number"], spec["key"], spec["description"][:70]))
                await ws.send(json.dumps({"t": "open_card", "card": c["number"]}))
                if exp["primary"] == "Не принята":
                    asyncio.create_task(status(c["number"], "Не принята", comment_for(exp, "sample_comment", numbers), think))
                else:
                    asyncio.create_task(status(c["number"], "Принята", "Принята, направляем бригаду", think))
            elif msg["t"] == "call":
                key = next((k for k, n in numbers.items() if n == msg["card"]), None)
                spec = by_key.get(key)
                rep = next((r for r in (spec or {}).get("brigade", []) if r["text"] == msg["text"]), None)
                if rep is None:
                    continue
                log("  доклад бригады по %s: %s" % (msg["card"], msg["text"]))
                comment = comment_for(spec["expect"], "sample_final_comment", numbers) \
                    if rep["status"] == "Отказ от выполнения работ" else msg["text"]
                asyncio.create_task(status(msg["card"], rep["status"], comment, think / 2))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8112")
    ap.add_argument("--pack", default="dds-shchukino-v0")
    ap.add_argument("--tempo", type=float, default=0.5, help="темп занятия: 1 — реальное время, 0.5 — вдвое быстрее")
    ap.add_argument("--teacher", default="teacher")
    ap.add_argument("--student", default="student2")
    ap.add_argument("--think", type=float, default=6.0, help="сколько секунд бот «думает» над карточкой")
    ap.add_argument("--wait", type=int, default=20, help="сколько секунд дать, чтобы открыть АРМ до старта")
    a = ap.parse_args()
    acc = {x["login"]: x["password"] for x in json.loads((ROOT / "data" / "demo" / "accounts.json").read_text(encoding="utf-8"))["accounts"]}
    pack = next((json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "data" / "scenarios").glob("*.json")
                 if json.loads(p.read_text(encoding="utf-8")).get("id") == a.pack), None)
    if pack is None:
        print("нет сценария %s в data/scenarios" % a.pack, file=sys.stderr)
        return 1
    t = call(a.server, "POST", "/api/login", {"login": a.teacher, "password": acc[a.teacher]})["token"]
    s = call(a.server, "POST", "/api/login", {"login": a.student, "password": acc[a.student]})
    stok, sid = s["token"], "u%d" % s["user"]["id"]
    lesson = call(a.server, "POST", "/api/lessons", {"pack_id": a.pack, "time_scale": a.tempo}, t)["id"]
    call(a.server, "POST", "/api/lessons/%s/join" % lesson, None, stok)
    call(a.server, "POST", "/api/lessons/%s/roles" % lesson, {"participant": sid, "service": pack["role"]}, t)
    last = max(c["at"] for c in pack["cards"]) + max((r["after"] for c in pack["cards"] for r in c.get("brigade", [])), default=0)
    duration = last * a.tempo + a.think * 2 + 20
    print("занятие %s, сценарий «%s», темп ×%s — около %d мин.\n"
          "Смотреть: откройте %s, войдите как %s и нажмите «Войти» у занятия %s (преподавателем — монитор).\n"
          "Старт через %d секунд…" % (lesson, pack["title"], a.tempo, duration // 60 + 1, a.server, a.student, lesson, a.wait), flush=True)
    time.sleep(a.wait)
    call(a.server, "POST", "/api/lessons/%s/start" % lesson, None, t)
    t0 = time.time()
    asyncio.run(bot(a.server, stok, lesson, pack, a.think, t0 + duration,
                    lambda m: print("[%s] %s" % (time.strftime("%M:%S", time.gmtime(time.time() - t0)), m), flush=True)))
    call(a.server, "POST", "/api/lessons/%s/finish" % lesson, None, t)
    res = call(a.server, "GET", "/api/lessons/%s/results" % lesson, None, t)["participants"][0]
    print("\nитог бота: %s баллов, замечаний %d%s" % (res["score"], len(res["findings"]),
          "".join("\n  №%s %s" % (f["category"], f["detail"]) for f in res["findings"])))
    print("отчёт о занятии: %s/report.html?lesson=%s (войти преподавателем)" % (a.server, lesson))
    return 0


if __name__ == "__main__":
    sys.exit(main())
