"""Контроль целостности программы (ТЗ, администратор: «контролировать целостность системы»).

При установке (manage.py integrity --write) снимается эталон: SHA-256 каждого файла программы и данных
(server, pycore, web, data, deploy). Проверка сравнивает с ним текущие файлы: изменённые, пропавшие,
лишние. Журнал аудита проверяется отдельно — цепочкой хешей (app.audit).
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Dict

ROOTS = ["server", "pycore", "web", "data", "deploy", "tools"]
SKIP_PARTS = {"__pycache__", ".pytest_cache"}
SKIP_DIRS = ("data/scenarios/",)  # учебный контент законно меняет администратор (пакетное обновление, аудит)
SKIP_SUFFIX = {".pyc", ".pyo"}


def _files(root: Path) -> Dict[str, Path]:
    out = {}
    for top in ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            rel = p.relative_to(root).as_posix()
            skip = SKIP_PARTS & set(p.parts) or p.suffix in SKIP_SUFFIX or rel.startswith(SKIP_DIRS)
            if p.is_file() and not skip:
                out[rel] = p
    return out


def _hash(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def snapshot(root, baseline) -> dict:
    root = Path(root)
    files = {rel: _hash(p) for rel, p in sorted(_files(root).items())}
    data = {"created_at": time.time(), "files": files}
    Path(baseline).parent.mkdir(parents=True, exist_ok=True)
    Path(baseline).write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
    return {"files": len(files), "created_at": data["created_at"]}


def check(root, baseline) -> dict:
    baseline = Path(baseline)
    if not baseline.is_file():
        return {"ok": None, "baseline": False,
                "message": "эталона нет — снимите его после установки: python server/manage.py integrity --write"}
    t0 = time.perf_counter()
    ref = json.loads(baseline.read_text(encoding="utf-8"))
    now = _files(Path(root))
    changed = sorted(rel for rel, digest in ref["files"].items() if rel in now and _hash(now[rel]) != digest)
    missing = sorted(rel for rel in ref["files"] if rel not in now)
    added = sorted(rel for rel in now if rel not in ref["files"])
    return {"ok": not (changed or missing or added), "baseline": True, "baseline_at": ref["created_at"],
            "checked": len(ref["files"]), "changed": changed[:200], "missing": missing[:200], "added": added[:200],
            "elapsed_s": round(time.perf_counter() - t0, 2)}
