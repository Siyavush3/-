"""Очередь озвучки на сервере.

Сервер не синтезирует сам: недостающие фразы копятся в очереди, а отдельный процесс speech.render
озвучивает их фоном с ограниченными ресурсами (профиль background — одно ядро, приоритет ниже обычного).
Процесс запускается из потока, а не из цикла событий: так не важно, какой цикл у uvicorn на Windows.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional

SERVER_DIR = Path(__file__).resolve().parents[1]


def _tail(path: str, limit: int = 300) -> str:
    """Конец журнала процесса озвучки. onnxruntime на Windows пишет в stderr в UTF-16."""
    data = Path(path).read_bytes()[-4 * limit:]
    text = data.decode("utf-16-le", errors="replace") if data.count(b"\0") > len(data) // 4 else \
        data.decode("utf-8", errors="replace")
    return " ".join(text.replace("\0", "").split())[-limit:]


class TtsQueue:
    def __init__(self, catalog, voices_dir, profile: str = "background", autostart: bool = True,
                 python: Optional[str] = None, module: str = "speech.render"):
        self.catalog = catalog
        self.voices_dir = Path(voices_dir)
        self.profile = profile
        self.module = module
        self.autostart = autostart
        self.python = python or sys.executable
        self.pending: Dict[str, dict] = {}
        self.rendered = 0
        self.failed = 0
        self.last: Optional[dict] = None   # итог последнего прохода процесса озвучки
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None

    def voices(self) -> List[str]:
        if not self.voices_dir.is_dir():
            return []
        return sorted(p.name[:-5] for p in self.voices_dir.glob("*.onnx") if (self.voices_dir / (p.name + ".json")).is_file())

    def available(self) -> bool:
        """Озвучка возможна: есть piper и хотя бы один голос. Сам piper сервер не импортирует."""
        return importlib.util.find_spec("piper") is not None and bool(self.voices())

    def enqueue(self, phrases: Iterable[dict]) -> int:
        voices = set(self.voices())
        missing = [p for p in self.catalog.missing(phrases) if p["voice"] in voices]
        with self._lock:
            for p in missing:
                self.pending[p["key"]] = p
        self.kick()
        return len(missing)

    def kick(self) -> None:
        if not (self.autostart and self.pending and self.available()):
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="tts-render", daemon=True)
            self._thread.start()

    def pause(self) -> None:
        """Остановить озвучку (администратор): текущий процесс завершается, очередь сохраняется."""
        self.autostart = False
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def resume(self) -> None:
        self.autostart = True
        self.kick()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            with self._lock:
                batch = list(self.pending.values())
            if not batch:
                return
            fd, jobs = tempfile.mkstemp(prefix="a112-tts-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(batch, f, ensure_ascii=False)
            log_fd, log_path = tempfile.mkstemp(prefix="a112-tts-", suffix=".log")
            cmd = [self.python, "-m", self.module, "--jobs", jobs, "--out", str(self.catalog.dir),
                   "--voices", str(self.voices_dir), "--profile", self.profile]
            env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SERVER_DIR), os.environ.get("PYTHONPATH", "")]),
                       PYTHONIOENCODING="utf-8")
            code = None
            try:
                with os.fdopen(log_fd, "wb") as log:
                    proc = subprocess.Popen(cmd, cwd=str(SERVER_DIR), env=env, stdout=subprocess.PIPE, stderr=log,
                                            text=True, encoding="utf-8", errors="replace",
                                            creationflags=0x08000000 if os.name == "nt" else 0)  # без окна консоли
                    self._proc = proc
                    for line in proc.stdout:
                        self._progress(line)
                    code = proc.wait()
            except OSError as exc:
                self.last_error = "не удалось запустить процесс озвучки: %s" % exc
            self._proc = None
            if not self.autostart:  # остановлено администратором — очередь ждёт запуска
                for path in (jobs, log_path):
                    os.unlink(path)
                return
            crashed = code not in (0, 1)
            if crashed and code is not None:
                self.last_error = "процесс озвучки завершился аварийно (код %s): %s" % (code, _tail(log_path))
            for path in (jobs, log_path):
                os.unlink(path)
            with self._lock:
                left = [p for p in sorted(batch, key=lambda j: j["voice"]) if p["key"] in self.pending]
                # Аварию вызвала фраза, на которой процесс остановился (он идёт по голосам в этом же порядке):
                # её считаем сбойной, остальные озвучиваем новым процессом. Без аварии повторять нечего.
                drop = left[:1] if crashed and code is not None else left
                for p in drop:
                    self.pending.pop(p["key"], None)
                self.failed += len(drop)

    def _progress(self, line: str) -> None:
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            msg = json.loads(line)
        except ValueError:
            return
        with self._lock:
            if msg.get("event") == "done":
                self.pending.pop(msg.get("key"), None)
                self.rendered += 1
            elif msg.get("event") == "error":
                self.pending.pop(msg.get("key"), None)
                self.failed += 1
                self.last_error = msg.get("error")
            elif msg.get("event") == "finish":
                self.last = {k: msg.get(k) for k in ("rendered", "speech_s", "synth_s", "realtime", "peak_mb")}

    def status(self) -> dict:
        return {"available": self.available(), "voices": self.voices(), "profile": self.profile,
                "pending": len(self.pending), "rendered": self.rendered, "failed": self.failed,
                "running": self.running(), "last": self.last, "last_error": self.last_error}
