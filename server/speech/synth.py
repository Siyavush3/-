"""Синтез речи piper (VITS в ONNX, onnxruntime на процессоре, без видеокарты).

Импортируется только процессом озвучки (speech.render): piper и espeak-ng распространяются по GPL-3.0,
сервер тренажёра их не загружает, а лишь читает готовые WAV.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

from speech.catalog import speakable, split_for_synthesis
from speech.paths import ascii_dir


def limit_blas(threads: int) -> None:
    """До импорта numpy: OpenBLAS резервирует буфер на каждое ядро машины — на 16 ядрах это ~480 МБ
    памяти, которые озвучке не нужны. Потоков BLAS — столько же, сколько потоков синтеза."""
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(name, str(max(1, int(threads))))


class PiperSynth:
    """max_voices — сколько голосов держать загруженными: голос занимает ~350 МБ, поэтому процесс
    озвучки сортирует фразы по голосам и держит в памяти один."""

    def __init__(self, voices_dir, threads: int = 1, max_voices: int = 1):
        limit_blas(threads)
        import onnxruntime  # noqa: F401 — ошибка импорта здесь понятнее, чем при первой фразе
        from piper.phonemize_espeak import ESPEAK_DATA_DIR
        self.dir = Path(voices_dir)
        self.threads = max(1, int(threads))
        self.max_voices = max(1, int(max_voices))
        self.espeak = ascii_dir(Path(ESPEAK_DATA_DIR))
        self._voices: Dict[str, object] = {}

    def voices(self):
        return sorted(p.name[:-5] for p in self.dir.glob("*.onnx"))

    def voice(self, name: str):
        if name not in self._voices:
            while len(self._voices) >= self.max_voices:  # освободить память под новый голос
                self._voices.pop(next(iter(self._voices)))
            import onnxruntime
            from piper import PiperVoice
            from piper.config import PiperConfig
            model = self.dir / (name + ".onnx")
            config = json.loads((self.dir / (name + ".onnx.json")).read_text(encoding="utf-8"))
            opts = onnxruntime.SessionOptions()
            opts.intra_op_num_threads = self.threads     # столько потоков, сколько разрешено озвучке
            opts.inter_op_num_threads = 1
            opts.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
            opts.log_severity_level = 3                   # в журнал — только ошибки
            # Без арены памяти: с ней onnxruntime копит память под реплики разной длины и не отдаёт —
            # за 60 фраз процесс вырастает до 1,2 ГБ; без неё держится около 200 МБ (синтез на ~20% медленнее).
            opts.enable_cpu_mem_arena = False
            opts.enable_mem_pattern = False
            session = onnxruntime.InferenceSession(model.read_bytes(), sess_options=opts,  # байты: путь может быть кириллицей
                                                   providers=["CPUExecutionProvider"])
            self._voices[name] = PiperVoice(session=session, config=PiperConfig.from_dict(config),
                                            espeak_data_dir=self.espeak)
        return self._voices[name]

    def synthesize(self, text: str, voice: str, scale: float = 1.0, noise: Optional[float] = None) -> Tuple[object, int]:
        """Звук фразы: float32 в диапазоне [-1, 1] и частота. Длинная фраза синтезируется по частям
        (split_for_synthesis) — так память процесса не зависит от длины реплики. noise=0 — без случайной
        вариации голоса: одна и та же фраза звучит одинаково (тесты распознавания речи)."""
        import numpy as np
        from piper import SynthesisConfig
        v = self.voice(voice)
        base = getattr(v.config, "length_scale", None) or 1.0
        config = SynthesisConfig(length_scale=base * scale, noise_scale=noise, noise_w_scale=noise)
        parts, rate = [], int(getattr(v.config, "sample_rate", 22050) or 22050)
        for piece, pause in split_for_synthesis(speakable(text)):
            for chunk in v.synthesize(piece, syn_config=config):
                rate = chunk.sample_rate
                parts.append(np.asarray(chunk.audio_float_array, dtype=np.float32))
            parts.append(np.zeros(int(pause * rate), dtype=np.float32))
        if parts:
            parts.pop()  # паузы после последней части не нужно
        return (np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)), rate
