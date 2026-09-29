"""Мониторинг (ТЗ, администратор: контролировать состояние всех компонентов в реальном времени,
отслеживать нагрузку на сервер, контролировать показатели работоспособности).

Нагрузка — psutil (процессор, память, диск, процесс сервера) и счётчики запросов; история за последний
час — по отсчёту раз в 10 секунд. Состояние компонентов — зелёный / жёлтый / красный с объяснением.
"""
from __future__ import annotations

import collections
import os
import time
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional

SAMPLE_S = 10.0
HISTORY = 360  # час по 10 секунд


class RequestStats:
    """Запросы за последнюю минуту (частота, доля ошибок, 95-й перцентиль времени) и счётчики по адресам."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.window: Deque[tuple] = collections.deque()
        self.by_route: Dict[str, int] = collections.Counter()
        self.total = 0
        self.errors = 0

    def add(self, route: str, status: int, ms: float) -> None:
        now = self.clock()
        self.window.append((now, status, ms))
        self.by_route[route] += 1
        self.total += 1
        if status >= 500:
            self.errors += 1
        while self.window and self.window[0][0] < now - 60:
            self.window.popleft()

    def minute(self) -> dict:
        now = self.clock()
        items = [x for x in self.window if x[0] >= now - 60]
        times = sorted(x[2] for x in items)
        p95 = times[min(len(times) - 1, int(0.95 * len(times)))] if times else None
        return {"rpm": len(items), "errors": sum(1 for x in items if x[1] >= 500),
                "p95_ms": round(p95, 1) if p95 is not None else None,
                "avg_ms": round(sum(times) / len(times), 1) if times else None}


class Monitor:
    def __init__(self, var_dir, clock: Callable[[], float] = time.time):
        import psutil
        self.psutil = psutil
        self.var_dir = Path(var_dir)
        self.clock = clock
        self.started = clock()
        self.proc = psutil.Process(os.getpid())
        self.proc.cpu_percent(None)
        psutil.cpu_percent(None)
        self.history: Deque[dict] = collections.deque(maxlen=HISTORY)
        self.requests = RequestStats(clock)

    def system(self) -> dict:
        ps = self.psutil
        vm = ps.virtual_memory()
        disk = ps.disk_usage(str(self.var_dir if self.var_dir.exists() else Path.cwd()))
        with self.proc.oneshot():
            rss = self.proc.memory_info().rss
            threads = self.proc.num_threads()
            cpu_proc = self.proc.cpu_percent(None) / max(1, ps.cpu_count() or 1)
        return {"cpu_percent": round(ps.cpu_percent(None), 1), "cpu_count": ps.cpu_count(),
                "memory_total_mb": round(vm.total / 2 ** 20), "memory_used_percent": round(vm.percent, 1),
                "disk_total_gb": round(disk.total / 2 ** 30, 1), "disk_free_gb": round(disk.free / 2 ** 30, 1),
                "disk_used_percent": round(disk.percent, 1),
                "process_rss_mb": round(rss / 2 ** 20, 1), "process_cpu_percent": round(cpu_proc, 1),
                "process_threads": threads, "uptime_s": round(self.clock() - self.started)}

    def sample(self, extra: Optional[dict] = None) -> dict:
        s = self.system()
        m = self.requests.minute()
        point = {"at": round(self.clock()), "cpu": s["cpu_percent"], "mem": s["memory_used_percent"],
                 "rss": s["process_rss_mb"], "rpm": m["rpm"], "p95": m["p95_ms"], "errors": m["errors"]}
        point.update(extra or {})
        self.history.append(point)
        return point


def health(components: List[dict]) -> str:
    """Итог по компонентам: худшее состояние."""
    order = {"ok": 0, "warn": 1, "fail": 2}
    return max((c["status"] for c in components), key=lambda s: order[s], default="ok")


def component(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}
