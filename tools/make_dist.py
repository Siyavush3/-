"""Сборка дистрибутива для изолированного контура (поставка, узел 8).

    python tools/make_dist.py --platform windows|linux [--python 3.11] [--no-voices] [--no-asr] [--no-wheels]

Запускается на машине сборки с интернетом. В zip попадает всё, что нужно на сервере класса без сети:
программа (server, pycore, web, data, deploy, docs), собранное C-ядро (если есть в build/bin для этой
платформы), колёса Python-пакетов для целевой платформы (pip download), голоса озвучки и архив модели
распознавания речи (распаковывается при установке с проверкой контрольной суммы). Установка —
deploy/windows/install.ps1 или deploy/linux/install.sh из распакованного архива.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDE = ["server", "pycore", "web", "data", "deploy", "docs", "requirements", "tools", "README.md", "CMakeLists.txt",
           "core", "pyproject.toml"]
SKIP = {"__pycache__", ".pytest_cache", "ui",          # docs/ui — снимки стенда заказчика, только локально
        "instr", "customer"}                          # data/instr, data/customer — тексты заказчика, программе не нужны
PLATFORM = {"windows": {"pip": ["win_amd64"], "core": ["arm112.dll"]},
            "linux": {"pip": ["manylinux2014_x86_64", "manylinux_2_17_x86_64", "manylinux_2_28_x86_64"],
                      "core": ["libarm112.so"]}}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# Чистый Python, на PyPI — только исходники: колесо (py3-none-any, подходит для любой платформы) собирается здесь
LOCAL_WHEELS = {"srt"}


def local_wheels(dest: Path, reqs) -> bool:
    specs = []
    for r in reqs:
        for line in (ROOT / "requirements" / r).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line.split("==")[0].strip().lower() in LOCAL_WHEELS:
                specs.append(line)
    if not specs:
        return True
    print("pip wheel:", " ".join(specs), flush=True)
    return subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(dest)] + specs).returncode == 0


def pip_download(dest: Path, reqs, platform: str, python: str) -> bool:
    if not local_wheels(dest, reqs):
        return False
    cmd = [sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--python-version", python.replace(".", ""),
           "--implementation", "cp", "--find-links", str(dest), "-d", str(dest)]
    for p in PLATFORM[platform]["pip"]:
        cmd += ["--platform", p]
    for r in reqs:
        cmd += ["-r", str(ROOT / "requirements" / r)]
    print("pip download:", " ".join(reqs), flush=True)
    return subprocess.run(cmd).returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", choices=sorted(PLATFORM), required=True)
    ap.add_argument("--python", default="3.11")
    ap.add_argument("--no-voices", action="store_true")
    ap.add_argument("--no-asr", action="store_true", help="без распознавания речи (модель Vosk, 46 МБ)")
    ap.add_argument("--no-wheels", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    a = ap.parse_args()
    sys.path[:0] = [str(ROOT / "pycore")]
    import arm112
    name = "arm112-%s-%s" % (arm112.__version__, a.platform)
    out = Path(a.out)
    stage = out / name
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)

    def ignore(folder, names):
        skip = [n for n in names if n in SKIP or n.endswith((".pyc", ".pyo"))]
        if Path(folder).resolve() == (ROOT / "data").resolve() and a.no_voices:
            skip.append("voices")
        if Path(folder).resolve() == (ROOT / "data" / "asr").resolve():
            # распакованная модель не нужна: архив меньше вдвое, установщик распакует и сверит SHA-256
            skip += [n for n in names if n.startswith("vosk-model-") and (a.no_asr or not n.endswith(".zip"))]
        return skip

    for item in INCLUDE:
        src = ROOT / item
        if src.is_dir():
            shutil.copytree(src, stage / item, ignore=ignore)
        elif src.is_file():
            shutil.copy2(src, stage / item)
    core = [f for f in PLATFORM[a.platform]["core"] if (ROOT / "build" / "bin" / f).is_file()]
    for f in core:
        (stage / "build" / "bin").mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "build" / "bin" / f, stage / "build" / "bin" / f)
    report = {"version": arm112.__version__, "platform": a.platform, "python": a.python, "built_at": time.time(),
              "core": core, "voices": sorted(p.name for p in (stage / "data" / "voices").glob("*.onnx")),
              "asr": sorted(p.name for p in (stage / "data" / "asr").glob("vosk-model-*.zip")), "wheels": {}}
    if not a.no_wheels:
        wheels = stage / "wheels"
        wheels.mkdir()
        report["wheels"]["server"] = pip_download(wheels, ["server.txt"], a.platform, a.python)
        report["wheels"]["tts"] = pip_download(wheels, ["tts.txt"], a.platform, a.python) if report["voices"] else False
        report["wheels"]["asr"] = pip_download(wheels, ["asr.txt"], a.platform, a.python) if report["asr"] else False
        if not report["wheels"]["server"]:
            print("не удалось скачать пакеты сервера — нужен интернет на машине сборки", file=sys.stderr)
            return 1
    (stage / "DIST.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    archive = out / (name + ".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, Path(name) / p.relative_to(stage))
    (out / (name + ".zip.sha256")).write_text("%s  %s\n" % (sha256(archive), archive.name), encoding="ascii")
    print("дистрибутив: %s (%.0f МБ); C-ядро: %s; голоса: %d; распознавание речи: %s; пакеты: %s" % (
        archive, archive.stat().st_size / 2 ** 20, ", ".join(core) or "нет (будет эталон на Python)",
        len(report["voices"]), "есть" if report["asr"] else "нет", "есть" if report["wheels"] else "нет"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
