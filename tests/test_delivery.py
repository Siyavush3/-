"""Поставка (узел 8): автопилот ДДС, сопроводительная документация, установщики, список библиотек."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from arm112.paths import data_dir
from arm112.reference.fsm import ResponseFsm
from arm112.reference.territory import Territory
from domain import generator as gen
from domain.engine import Engine
from domain.evaluation import Rubric
from domain.scenario import CardViews, Pack, Router
from tests_support import perfect_run

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# ---------- автопилот ДДС (tools/demo_dds.py) ----------
def test_autopilot_comment_uses_sample_or_required_facts():
    demo_dds = pytest.importorskip("demo_dds")
    exp = {"sample_comment": "Дубль карточки {A}.", "comment_facts": [["дубль"]],
           "final_comment_facts": [["отказ", "не наша сеть"], ["ОЭК"]]}
    assert demo_dds.comment_for(exp, "sample_comment", {"A": "36901101"}) == "Дубль карточки 36901101."
    # образца нет — комментарий из первых вариантов обязательных фактов
    assert demo_dds.comment_for(exp, "sample_final_comment", {}) == "отказ; ОЭК"


def test_autopilot_scores_100_on_builtin_dds_pack(classifier):
    """Бот-диспетчер из tools/demo_dds.py на встроенном сценарии ДДС: ровно по эталону — 100 баллов,
    и его комментарии проходят проверку текста, как у сервера."""
    pytest.importorskip("pymorphy3")
    demo_dds = pytest.importorskip("demo_dds")
    from domain.textcheck import TextChecker, domain_words
    with open(data_dir() / "classifier" / "ui_services.json", encoding="utf-8") as f:
        router = Router(classifier, Territory.load(), json.load(f))
    packs = [Pack.load(p) for p in sorted((data_dir() / "scenarios").glob("dds_*.json"))]
    assert packs, "нет встроенных сценариев ДДС"
    with open(data_dir() / "services" / "services.json", encoding="utf-8") as f:
        services = [x["short"] for x in json.load(f)["services"]]
    checker = TextChecker(domain_words(classifier, packs, services) | set(gen.vocabulary(gen.Gazetteer.load())))
    for pack in packs:
        pack.validate(classifier, router.territory, ResponseFsm.load(), router)
        clock = Clock()
        engine = Engine(router, CardViews.load(classifier), ResponseFsm.load(), Rubric.load(), {pack.id: pack},
                        clock=clock, wall=lambda: 1_758_000_000.0, textcheck=checker)
        result = perfect_run(engine, clock, pack, comment=demo_dds.comment_for)
        assert result["score"] == 100, (pack.id, result["findings"])


# ---------- сопроводительная документация (tools/build_docs.py) ----------
def test_markdown_subset_renders_to_word():
    docx = pytest.importorskip("docx")
    import build_docs
    doc = docx.Document()
    build_docs.setup(doc)
    build_docs.markdown(doc, "# Название файла\n\n## Раздел, см. [ROADMAP.md](ROADMAP.md)\n\n"
                             "Абзац **жирный `код`** и `код`,\nпродолжение.\n\n"
                             "1. Первый\n   продолжение первого\n2. Второй\n\n- пункт\n  хвост\n\n"
                             "| А | Б |\n|---|---|\n| 1 | 2 \\| 3 |\n\n```\nкод как есть\n```\n", base="docs")
    texts = [p.text for p in doc.paragraphs]
    assert "Название файла" not in texts                  # его заменяет заголовок раздела документа
    assert "Раздел, см. ROADMAP.md (раздел %d)" % build_docs.LINKS["docs/ROADMAP.md"] in texts
    assert "Абзац жирный код и код, продолжение." in texts
    assert "1.\tПервый продолжение первого" in texts and "2.\tВторой" in texts
    assert "пункт хвост" in texts
    assert [c.text for c in doc.tables[0].rows[1].cells] == ["1", "2 | 3"]
    assert any(p.style.name == "Код" and p.text == "код как есть" for p in doc.paragraphs)


def test_delivery_document_contains_every_repository_doc(tmp_path):
    docx = pytest.importorskip("docx")
    import build_docs
    out = tmp_path / "doc.docx"
    build_docs.build(out)
    doc = docx.Document(out)
    paras = doc.paragraphs
    h1 = [p.text for p in paras if p.style.name == "Heading 1"]
    assert h1 == ["%d. %s" % (n, title) for n, (title, _, _) in enumerate(build_docs.sections(), 1)]
    # оглавление читается и без обновления полей Word
    toc = [p.text for p in paras if p.style.name in ("toc 1", "toc 2")]
    assert toc == [p.text for p in paras if p.style.name in ("Heading 1", "Heading 2")]
    texts = {p.text for p in paras}
    cells = {c.text for t in doc.tables for r in t.rows for c in r.cells}
    body = [t for t in texts if t] + list(cells)
    code = {p.text for p in paras if p.style.name == "Код"}
    for marker in ("**", "](", "|---", "```", "<!--", "![", "*("):
        assert not [t for t in body if marker in t], marker
    assert not [t for t in body if "`" in t and t not in code]
    # рисунки: каждый из документов — в .docx, с подписью
    images = [m for _, text, _ in build_docs.sections() for m in re.findall(r"^!\[([^\]]*)\]\(([^)]+)\)$", text, re.M)]
    assert len(images) >= 9 and len(doc.inline_shapes) == len(images)
    assert all(any(p.startswith("Рисунок %d — %s" % (n, cap)) for p in texts) for n, (cap, _) in enumerate(images, 1))
    for rel in ("docs/USER_GUIDE.md", "docs/ADMIN_GUIDE.md", "deploy/README.md", "docs/SYSTEM.md", "docs/API.md",
                "docs/SCENARIOS.md", "docs/DEVELOPER.md", "docs/QUICK_TEST.md", "docs/ROADMAP.md",
                "docs/LOADTEST.md", "docs/ASSUMPTIONS.md", "docs/LIBRARIES.md"):
        build_docs.Ctx.base = rel.rsplit("/", 1)[0]          # ссылки в заголовках — относительно файла
        for line in (ROOT / rel).read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                assert build_docs.plain(line[3:]) in texts, (rel, line)
    ids = re.findall(r"^\| (\d+\.\d+) \|", (ROOT / "docs" / "ASSUMPTIONS.md").read_text(encoding="utf-8"), re.M)
    assert len(ids) > 60 and set(ids) <= cells                # все допущения — в таблицах документа


# ---------- установщики и зависимости ----------
def test_requirements_declare_utf8():
    """pip на Windows читает файл требований в кодировке системы — без объявления кириллица ломает установку."""
    for f in (ROOT / "requirements").glob("*.txt"):
        text = f.read_bytes()
        if any(b > 127 for b in text):
            assert text.startswith(b"# -*- coding: utf-8 -*-"), f.name


def test_windows_installer_encoding():
    """Windows PowerShell 5.1 читает скрипт без BOM в кодировке системы; строки start-server.cmd — латиницей
    (cmd.exe читает файл в OEM-кодировке)."""
    raw = (ROOT / "deploy" / "windows" / "install.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    lines = re.search(r"^\$envLines = @\((.*)\)$", text, re.M).group(1)
    assert lines.isascii(), lines


def test_libraries_list_is_complete():
    """docs/LIBRARIES.md (tools/licenses.py) перечисляет каждый пакет из файлов требований."""
    listed = (ROOT / "docs" / "LIBRARIES.md").read_text(encoding="utf-8").lower()
    for f in (ROOT / "requirements").glob("*.txt"):
        for line in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r"([A-Za-z0-9_.-]+)", line.split("#", 1)[0].strip())
            if m and not line.startswith("-"):
                assert "| %s |" % m.group(1).lower() in listed, (f.name, m.group(1))


# ---------- справочные разделы документации (tools/build_reference.py) ----------
def test_reference_docs_are_up_to_date():
    """API, настройки, команды, критерии оценки, статусы и схема базы в документах совпадают с кодом."""
    res = subprocess.run([sys.executable, str(ROOT / "tools" / "build_reference.py"), "--check"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert res.returncode == 0, res.stdout + res.stderr


def test_every_http_route_is_documented():
    import build_reference
    from app.main import create_app
    text = build_reference.api_md(create_app())               # маршрут без описания — SystemExit
    for method, path in build_reference.SUMMARY:
        assert "| `%s` | `%s` |" % (method, path) in text, (method, path)   # описания без маршрута не бывает
