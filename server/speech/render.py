"""Процесс озвучки.

    python -m speech.render --jobs JOBS.json --out var/tts --voices data/voices [--profile background]

JOBS.json — список фраз {key, text, voice, scale}. Недостающие фразы синтезируются piper, проходят через
«телефонную линию» и пишутся в OUT/<key>.wav. Прогресс — по строке JSON на фразу в stdout: сервер
читает его и показывает в мониторинге. Ресурсы ограничиваются до загрузки модели (speech.limits).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from speech import limits
from speech.dsp import PHONE_RATE, phone_line, write_wav


def emit(event: str, **data) -> None:
    print(json.dumps(dict(event=event, **data), ensure_ascii=False), flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="озвучка реплик тренажёра (piper)")
    ap.add_argument("--jobs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--voices", required=True)
    ap.add_argument("--profile", choices=sorted(limits.PROFILES), default="background")
    ap.add_argument("--threads", type=int)
    ap.add_argument("--cpus", type=int)
    ap.add_argument("--memory-mb", type=int)
    args = ap.parse_args(argv)
    prof = dict(limits.PROFILES[args.profile])
    for name in ("threads", "cpus", "memory_mb"):
        if getattr(args, name) is not None:
            prof[name] = getattr(args, name)
    applied = limits.apply(prof["cpus"], prof["memory_mb"], prof["low_priority"])
    jobs = json.loads(Path(args.jobs).read_text(encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    emit("start", jobs=len(jobs), profile=args.profile, threads=prof["threads"], limits=applied)

    from speech.synth import PiperSynth
    t_load = time.perf_counter()
    synth = PiperSynth(args.voices, threads=prof["threads"], max_voices=1)
    speech_s = synth_s = 0.0
    done = failed = 0
    for job in sorted(jobs, key=lambda j: j["voice"]):  # по голосам: в памяти всегда один голос
        path = out / (job["key"] + ".wav")
        if path.is_file():
            continue
        try:
            t0 = time.perf_counter()
            audio, rate = synth.synthesize(job["text"], job["voice"], float(job.get("scale", 1.0)))
            t1 = time.perf_counter()
            pcm = phone_line(audio, rate)
            write_wav(path, pcm, PHONE_RATE)
        except Exception as exc:  # одна плохая фраза не должна останавливать озвучку остальных
            failed += 1
            emit("error", key=job["key"], error="%s: %s" % (type(exc).__name__, exc))
            continue
        done += 1
        seconds = len(pcm) / PHONE_RATE
        speech_s += seconds
        synth_s += t1 - t0
        emit("done", key=job["key"], speech_s=round(seconds, 2), synth_s=round(t1 - t0, 3))
    emit("finish", rendered=done, failed=failed, speech_s=round(speech_s, 1), synth_s=round(synth_s, 2),
         total_s=round(time.perf_counter() - t_load, 2),
         realtime=round(speech_s / synth_s, 1) if synth_s else None, peak_mb=limits.peak_memory_mb())
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
