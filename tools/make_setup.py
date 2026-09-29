r"""ARM112-Setup.exe — установщик для Windows со встроенным Python (поставка, узел 8).

    python tools/make_setup.py [--iscc ПУТЬ] [--no-voices] [--no-asr]

Запускается на машине сборки: Windows x64, Python 3.11 (пакеты ставятся его pip), интернет — для пакетов и
встроенного Python с python.org (или всё уже в кэше dist/cache). В dist/setup/stage собирается папка программы:
файлы тренажёра, C-ядро, голоса, модель распознавания и python\ — встроенный Python 3.11 со всеми пакетами
сервера, озвучки и распознавания. Inno Setup (deploy/windows/setup.iss) упаковывает её в
dist/ARM112-Setup-<версия>.exe. На целевой машине не нужны ни Python, ни интернет: мастер спрашивает папку,
порт и пароль администратора, настройку делает deploy/windows/install.ps1 с готовым Python (-Python).
Inno Setup: переменная ISCC, ключ --iscc или dist/tools/innosetup (переносимая установка).
"""
import argparse
import os
import shutil
import struct
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "pycore")]
from make_dist import INCLUDE, SKIP, local_wheels, sha256  # noqa: E402

EMBED = "3.11.9"   # последний выпуск 3.11 с готовыми сборками для Windows; колёса cp311 те же, что у сервера
EMBED_URL = "https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip" % (EMBED, EMBED)
EMBED_SHA256 = "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b"
REQUIREMENTS = ["server.txt", "tts.txt", "asr.txt"]
# ._pth встроенного Python задаёт sys.path целиком (PYTHONPATH он не читает): процесс озвучки
# (python -m speech.render) находит модули сервера по строкам ..\server и ..\pycore
PTH = ["python311.zip", ".", r"Lib\site-packages", r"..\server", r"..\pycore", "import site"]
# DLL, которые есть в любой Windows 10/11 (плюс наборы API api-ms-win-*: универсальная среда выполнения C)
SYSTEM_DLLS = {
    "kernel32.dll", "user32.dll", "advapi32.dll", "ws2_32.dll", "shell32.dll", "ole32.dll", "oleaut32.dll",
    "gdi32.dll", "crypt32.dll", "bcrypt.dll", "ntdll.dll", "shlwapi.dll", "version.dll", "winmm.dll", "iphlpapi.dll",
    "secur32.dll", "userenv.dll", "dbghelp.dll", "psapi.dll", "setupapi.dll", "cfgmgr32.dll", "powrprof.dll",
    "comdlg32.dll", "comctl32.dll", "rpcrt4.dll", "msvcrt.dll", "ucrtbase.dll", "dwmapi.dll", "imm32.dll",
    "netapi32.dll", "mswsock.dll", "dnsapi.dll", "wtsapi32.dll", "pdh.dll", "ncrypt.dll", "wldap32.dll",
    "normaliz.dll", "shcore.dll", "uxtheme.dll", "d3d11.dll", "dxgi.dll", "d3d12.dll", "directml.dll", "opengl32.dll",
    "combase.dll", "sechost.dll", "wintrust.dll", "cabinet.dll", "winhttp.dll", "wininet.dll", "mpr.dll",
    "bcryptprimitives.dll", "msi.dll",
}
# Среда выполнения Visual C++: в Windows её нет, её ставит распространяемый пакет Microsoft (vc_redist.x64.exe).
# Нужна onnxruntime (озвучка); установщик запускает пакет, только если msvcp140.dll в системе нет.
VC_DLLS = {"msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll",
           "concrt140.dll", "vcomp140.dll"}
VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"


def pe_imports(path: Path):
    """Имена DLL из таблицы импорта PE-файла (.dll, .pyd, .exe) — без внешних инструментов."""
    data = path.read_bytes()
    if data[:2] != b"MZ":
        return []
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return []
    sections_n = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    dirs = opt + (112 if magic == 0x20B else 96)
    imp_rva = struct.unpack_from("<I", data, dirs + 8)[0]          # каталог 1 — импорт
    delay_rva = struct.unpack_from("<I", data, dirs + 13 * 8)[0]   # каталог 13 — отложенный импорт
    sections = []
    for i in range(sections_n):
        s = opt + opt_size + i * 40
        vsize, va, rsize, raw = struct.unpack_from("<IIII", data, s + 8)
        sections.append((va, max(vsize, rsize), raw))

    def off(rva):
        for va, size, raw in sections:
            if va <= rva < va + size:
                return rva - va + raw
        return None

    def name_at(rva):
        o = off(rva)
        return data[o:data.index(b"\0", o)].decode("ascii", "replace") if o is not None else ""

    names = []
    o = off(imp_rva) if imp_rva else None
    while o is not None and o + 20 <= len(data):
        name_rva = struct.unpack_from("<I", data, o + 12)[0]
        if not name_rva:
            break
        names.append(name_at(name_rva))
        o += 20
    o = off(delay_rva) if delay_rva else None
    while o is not None and o + 32 <= len(data):
        name_rva = struct.unpack_from("<I", data, o + 4)[0]
        if not name_rva:
            break
        names.append(name_at(name_rva))
        o += 32
    return names


def missing_dlls(stage: Path):
    """DLL, которых нет ни в сборке, ни в любой Windows: без них модуль не загрузится у заказчика."""
    files = [p for p in stage.rglob("*") if p.suffix.lower() in (".dll", ".pyd", ".exe") and p.is_file()]
    have = {p.name.lower() for p in files}
    missing = {}
    for p in files:
        for dll in pe_imports(p):
            d = dll.lower()
            if d in have or d in SYSTEM_DLLS or d.startswith(("api-ms-win-", "ext-ms-")):
                continue
            missing.setdefault(d, []).append(str(p.relative_to(stage)))
    return missing


def signed_by(exe: Path, signer: str) -> bool:
    """Действительная подпись Authenticode с этим издателем (проверка PowerShell)."""
    cmd = ["powershell", "-NoProfile", "-Command",
           "$s = Get-AuthenticodeSignature -LiteralPath '%s'; if ($s.Status -eq 'Valid') { $s.SignerCertificate.Subject }" % exe]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    return ("CN=" + signer) in out


def find_iscc(explicit):
    for cand in (explicit, os.environ.get("ISCC"), ROOT / "dist" / "tools" / "innosetup" / "ISCC.exe",
                 Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
                 Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe"):
        if cand and Path(cand).is_file():
            return Path(cand)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iscc", help="путь к ISCC.exe (Inno Setup 6)")
    ap.add_argument("--no-voices", action="store_true", help="без голосов озвучки (−190 МБ)")
    ap.add_argument("--no-asr", action="store_true", help="без распознавания речи (−90 МБ)")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    a = ap.parse_args()
    if os.name != "nt" or sys.version_info[:2] != (3, 11):
        print("сборка установщика — на Windows с Python 3.11 (пакеты ставит его pip)", file=sys.stderr)
        return 1
    iscc = find_iscc(a.iscc)
    if iscc is None:
        print("нет Inno Setup 6: поставьте его или укажите --iscc / переменную ISCC", file=sys.stderr)
        return 1
    import arm112
    out = Path(a.out)
    work = out / "setup"
    stage = work / "stage"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)

    # 1. программа (тот же состав, что у zip-дистрибутива), C-ядро, голоса, распакованная модель распознавания
    def ignore(folder, names):
        skip = [n for n in names if n in SKIP or n.endswith((".pyc", ".pyo"))]
        here = Path(folder).resolve()
        if here == (ROOT / "data").resolve() and a.no_voices:
            skip.append("voices")
        if here == (ROOT / "data" / "asr").resolve():
            skip += [n for n in names if n.startswith("vosk-model-") and (a.no_asr or n.endswith(".zip"))]
        return skip

    for item in INCLUDE:
        src = ROOT / item
        if src.is_dir():
            shutil.copytree(src, stage / item, ignore=ignore)
        elif src.is_file():
            shutil.copy2(src, stage / item)
    core = ROOT / "build" / "bin" / "arm112.dll"
    if core.is_file():
        (stage / "build" / "bin").mkdir(parents=True, exist_ok=True)
        shutil.copy2(core, stage / "build" / "bin" / core.name)

    # 2. встроенный Python 3.11 с python.org: проверка контрольной суммы и подписи
    cache = out / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    embed = cache / ("python-%s-embed-amd64.zip" % EMBED)
    if not embed.is_file() or sha256(embed) != EMBED_SHA256:
        print("загрузка", EMBED_URL, flush=True)
        urllib.request.urlretrieve(EMBED_URL, str(embed))
    if sha256(embed) != EMBED_SHA256:
        print("встроенный Python: контрольная сумма не совпала", file=sys.stderr)
        return 1
    py = stage / "python"
    with zipfile.ZipFile(embed) as z:
        z.extractall(py)
    if not signed_by(py / "python.exe", "Python Software Foundation"):
        print("python.exe не подписан Python Software Foundation — сборка остановлена", file=sys.stderr)
        return 1
    (py / "python311._pth").write_text("\n".join(PTH) + "\n", encoding="ascii")
    site = py / "Lib" / "site-packages"
    site.mkdir(parents=True)

    # 3. пакеты сервера, озвучки и распознавания — прямо в site-packages встроенного Python
    reqs = [r for r in REQUIREMENTS if not (r == "tts.txt" and a.no_voices) and not (r == "asr.txt" and a.no_asr)]
    wheels = work / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    if not local_wheels(wheels, reqs):
        return 1
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
           "--only-binary=:all:", "--find-links", str(wheels), "--target", str(site)]
    for r in reqs:
        cmd += ["-r", str(ROOT / "requirements" / r)]
    print("pip install --target python\\Lib\\site-packages:", " ".join(reqs), flush=True)
    if subprocess.run(cmd).returncode:
        print("пакеты не установились", file=sys.stderr)
        return 1
    shutil.rmtree(site / "bin", ignore_errors=True)   # консольные обёртки пакетов тренажёру не нужны
    # тесты пакетов и инструменты конвертации моделей onnxruntime серверу не нужны: минус объём и самые длинные
    # пути (Inno Setup, как и многие программы Windows, не работает с путями длиннее 260 символов)
    for d in [d for d in site.rglob("tests") if d.is_dir()] + [site / "onnxruntime" / "tools"]:
        shutil.rmtree(d, ignore_errors=True)

    # 4. проверка: встроенный Python находит модули сервера и пакеты, DLL — все на месте
    probe = subprocess.run([str(py / "python.exe"), "-c",
                            "import speech.asr, domain.calls, fastapi, uvicorn, pymorphy3, arm112; "
                            "print(arm112.__version__)"], capture_output=True, text=True, cwd=str(stage))
    if probe.returncode:
        print("встроенный Python не загрузил модули:\n" + probe.stderr, file=sys.stderr)
        return 1
    lost = missing_dlls(stage)
    vc = sorted(d for d in lost if d in VC_DLLS)
    for dll, users in sorted(lost.items()):
        if dll not in VC_DLLS:
            print("  нет %s — нужна для %s" % (dll, ", ".join(users[:3])), file=sys.stderr)
    if len(vc) < len(lost):
        print("не хватает DLL — у заказчика эти модули не загрузятся", file=sys.stderr)
        return 1
    defines = []
    if vc:  # среда выполнения Visual C++ — пакетом Microsoft внутри установщика
        redist = cache / "vc_redist.x64.exe"
        if not redist.is_file():
            print("загрузка", VC_REDIST_URL, flush=True)
            urllib.request.urlretrieve(VC_REDIST_URL, str(redist))
        if not signed_by(redist, "Microsoft Corporation"):
            print("vc_redist.x64.exe не подписан Microsoft — сборка остановлена", file=sys.stderr)
            return 1
        defines.append("/DVCRedist=" + str(redist.resolve()))
        print("среда Visual C++ (%s) — пакет Microsoft в установщике" % ", ".join(vc), flush=True)

    # самый длинный путь внутри программы: мастер не даст выбрать папку, с которой пути вышли бы за 250 символов
    longest = max(len(str(q.relative_to(stage))) for q in stage.rglob("*"))
    defines.append("/DMaxDirLen=%d" % (250 - longest))
    print("самый длинный путь внутри программы — %d символов: папка установки не длиннее %d" % (longest, 250 - longest), flush=True)

    # 5. Inno Setup
    print("Inno Setup: %s" % iscc, flush=True)
    res = subprocess.run([str(iscc), "/Q", "/DAppVersion=" + arm112.__version__, "/DStage=" + str(stage.resolve()),
                          "/DOutDir=" + str(out.resolve())] + defines + [str(ROOT / "deploy" / "windows" / "setup.iss")])
    if res.returncode:
        return 1
    exe = out / ("ARM112-Setup-%s.exe" % arm112.__version__)
    (out / (exe.name + ".sha256")).write_text("%s  %s\n" % (sha256(exe), exe.name), encoding="ascii")
    size = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file())
    print("установщик: %s (%.0f МБ; в установленном виде %.0f МБ)" % (exe, exe.stat().st_size / 2 ** 20, size / 2 ** 20))
    return 0


if __name__ == "__main__":
    sys.exit(main())
