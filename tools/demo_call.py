"""Демонстрационный звонок в 112 и доклад старшего бригады — послушать качество озвучки и замерить скорость.

    python tools/demo_call.py [--profile demo] [--out build/tts]

Ресурсы ограничиваются так же, как у фоновой озвучки на демо-стенде (speech.limits, профиль demo):
4 ядра i5, приоритет ниже обычного, потолок памяти 1 ГБ, синтез в один поток. На демо-ноутбуке
эта же команда покажет реальную скорость.

Реплики оператора озвучены только здесь, для понятности записи: на занятии оператор — сам обучающийся,
а голосом говорят заявитель и старший бригады. Заявитель и бригада звучат «по телефонной линии».
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):  # до numpy: см. speech.synth.limit_blas
    os.environ.setdefault(_name, "1")
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "pycore")]

from speech import limits  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="demo", choices=sorted(limits.PROFILES))
    ap.add_argument("--out", default=str(ROOT / "build" / "tts"))
    ap.add_argument("--voices", default=str(ROOT / "data" / "voices"))
    args = ap.parse_args()
    prof = limits.PROFILES[args.profile]
    applied = limits.apply(prof["cpus"], prof["memory_mb"], prof["low_priority"])

    import numpy as np
    from domain import generator as gen
    from speech.catalog import BRIGADE, caller_voice
    from speech.dsp import PHONE_RATE, phone_line, resample, write_wav
    from speech.synth import PiperSynth

    gz = gen.Gazetteer.load()
    call = next(c for seed in range(200) for c in gen.generate_calls(gz, ["14"], 2, 2, seed)["calls"]
                if c["gold"]["codes"][0] == "14020300" and c["caller"]["gender"] == "f")
    d = call["dialogue"]
    caller = caller_voice(call)
    operator = "ru_RU-dmitri-medium"
    lines = [("op", "Служба сто двенадцать, оператор слушает."), ("caller", d["opening"]),
             ("op", "Назовите адрес."), ("caller", d["address"]),
             ("op", "Уточните, пожалуйста, улицу и номер дома."), ("caller", d["address_clarified"]),
             ("op", "Как вас зовут?"), ("caller", d["name"]),
             ("op", "Есть пострадавшие?"), ("caller", d["victims"]),
             ("op", "Информацию передаю в аварийную службу, ожидайте.")]
    synth = PiperSynth(args.voices, threads=prof["threads"], max_voices=2)  # заявитель и оператор по очереди
    rate = 22050
    stats = {"speech_s": 0.0, "synth_s": 0.0, "phrases": 0}

    def say(text, voice, scale=1.0, phone=True):
        t0 = time.perf_counter()
        audio, r = synth.synthesize(text, voice, scale)
        stats["synth_s"] += time.perf_counter() - t0
        stats["speech_s"] += len(audio) / r
        stats["phrases"] += 1
        if phone:  # через «телефонную линию» и обратно в 22 кГц, чтобы склеить с голосом оператора
            return resample(phone_line(audio, r).astype(np.float64) / 32767, PHONE_RATE, rate)
        return resample(audio, r, rate) * 0.8

    def ring(times=2):
        t = np.arange(int(0.6 * rate)) / rate
        tone = 0.35 * np.sin(2 * np.pi * np.where((t * 32).astype(int) % 2 == 0, 800, 1000) * t) * (t < 0.4)
        return np.concatenate([tone] * times + [np.zeros(int(0.3 * rate))])

    gap = np.zeros(int(0.45 * rate))
    parts = [ring()]
    for who, text in lines:
        parts += [say(text, caller.voice, caller.scale) if who == "caller" else say(text, operator, phone=False), gap]
    conversation = np.concatenate(parts)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(np.round(conversation * 32767), -32768, 32767).astype("<i2")
    write_wav(out / "zvonok_112_demo.wav", pcm, rate)

    reports = [r["text"] for r in gen._brigade("water", "ГБУ «Жилищник района Щукино»")[:3]]
    brig = np.concatenate([np.concatenate([phone_line(*synth.synthesize(t, BRIGADE.voice, BRIGADE.scale)),
                                           np.zeros(int(0.6 * PHONE_RATE), dtype="<i2")]) for t in reports])
    stats["phrases"] += len(reports)
    write_wav(out / "doklad_brigady_demo.wav", brig, PHONE_RATE)

    report = {"profile": args.profile, "limits": applied, "threads": prof["threads"], "call": call["key"],
              "caller_voice": caller.voice, "phrases": stats["phrases"], "speech_s": round(stats["speech_s"], 1),
              "synth_s": round(stats["synth_s"], 2), "realtime": round(stats["speech_s"] / stats["synth_s"], 1),
              "peak_mb": limits.peak_memory_mb(), "files": ["zvonok_112_demo.wav", "doklad_brigady_demo.wav"],
              "conversation_s": round(len(pcm) / rate, 1)}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
