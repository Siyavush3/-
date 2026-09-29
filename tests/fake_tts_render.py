"""Подставной процесс озвучки для тестов очереди: вместо речи пишет 0,1 с тишины.
Фраза со словом «сбой» — ошибка синтеза этой фразы, со словом «авария» — аварийное завершение процесса."""
import json
import os
import sys
from pathlib import Path

from speech.dsp import write_wav


def arg(name):
    return sys.argv[sys.argv.index(name) + 1]


jobs = json.loads(Path(arg("--jobs")).read_text(encoding="utf-8"))
out = Path(arg("--out"))
print(json.dumps({"event": "start", "jobs": len(jobs), "profile": arg("--profile")}), flush=True)
done = 0
for job in sorted(jobs, key=lambda j: j["voice"]):  # в том же порядке, что настоящий процесс
    if "авария" in job["text"]:
        sys.stderr.write("bad allocation\n")
        sys.stderr.flush()
        os._exit(3)
    if "сбой" in job["text"]:
        print(json.dumps({"event": "error", "key": job["key"], "error": "RuntimeError: тест"}), flush=True)
        continue
    write_wav(out / (job["key"] + ".wav"), b"\0\0" * 800, 8000)
    done += 1
    print(json.dumps({"event": "done", "key": job["key"], "speech_s": 0.1, "synth_s": 0.01}), flush=True)
print(json.dumps({"event": "finish", "rendered": done, "speech_s": 0.1 * done, "synth_s": 0.01 * done,
                  "realtime": 10.0, "peak_mb": 50.0}), flush=True)
