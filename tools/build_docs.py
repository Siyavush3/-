"""Сопроводительная документация (.docx) из документов репозитория (поставка, узел 8).

    python tools/build_docs.py [--out docs/Сопроводительная документация.docx]

Источник один — Markdown-файлы репозитория (docs/*.md, deploy/README.md, README.md) с рисунками из docs/img;
справочные таблицы в них собирает из кода tools/build_reference.py. Документ пересобирается после каждого
изменения, поэтому не расходится с программой. Нужен пакет python-docx (requirements/dev.txt).
"""
import argparse
import posixpath
import re
import sys
import time
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pycore")]
import arm112  # noqa: E402

OUT = ROOT / "docs" / "Сопроводительная документация.docx"
FONT, MONO = "Arial", "Consolas"
INK, BLUE, MUTED = RGBColor(0x1D, 0x23, 0x27), RGBColor(0x15, 0x5A, 0x8A), RGBColor(0x5D, 0x6A, 0x72)
HEAD_FILL, CODE_FILL = "E8EDF1", "F3F5F6"
WIDTH_CM = 17.0                                   # A4 без полей по 2 см

def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def readme_part(name: str) -> str:
    """Раздел корневого README по заголовку второго уровня."""
    m = re.search(r"^## %s\n(.*?)(?=^## |\Z)" % re.escape(name), read("README.md"), re.S | re.M)
    return m.group(1)


def sections():
    """(заголовок, Markdown, файл-источник) — порядок частей документа: от пользователя к разработчику,
    дальше — проверка, допущения и лицензии."""
    intro = read("README.md").split("\n## Быстрый старт")[0].split("\n", 1)[1]
    install = read("deploy/README.md") + "\n## 10. Переменные окружения\n" + readme_part("Переменные окружения")
    doc = lambda rel: read(rel)  # noqa: E731
    return [
        ("Общие сведения", intro, "README.md"),
        ("Руководство пользователя", doc("docs/USER_GUIDE.md"), "docs/USER_GUIDE.md"),
        ("Руководство администратора", doc("docs/ADMIN_GUIDE.md"), "docs/ADMIN_GUIDE.md"),
        ("Установка и обновление", install, "deploy/README.md"),
        ("Описание системы", doc("docs/SYSTEM.md"), "docs/SYSTEM.md"),
        ("Программный интерфейс", doc("docs/API.md"), "docs/API.md"),
        ("Формат сценариев", doc("docs/SCENARIOS.md"), "docs/SCENARIOS.md"),
        ("Руководство разработчика", doc("docs/DEVELOPER.md"), "docs/DEVELOPER.md"),
        ("Проверка работы одним человеком", doc("docs/QUICK_TEST.md"), "docs/QUICK_TEST.md"),
        ("Что реализовано и как проверить", doc("docs/ROADMAP.md"), "docs/ROADMAP.md"),
        ("Нагрузочное тестирование", doc("docs/LOADTEST.md"), "docs/LOADTEST.md"),
        ("Допущения, принятые при разработке", doc("docs/ASSUMPTIONS.md"), "docs/ASSUMPTIONS.md"),
        ("Сторонние библиотеки и лицензии", doc("docs/LIBRARIES.md"), "docs/LIBRARIES.md"),
    ]


# Ссылка на документ, вошедший в .docx, получает номер раздела: «ROADMAP.md (раздел 8)».
LINKS = {src: n for n, (_, _, src) in enumerate(sections(), 1) if src and src != "README.md"}


class Ctx:
    base = ""                                     # каталог текущего Markdown-файла — для относительных ссылок
    figures = 0                                   # нумерация рисунков


def link_text(text: str, target: str) -> str:
    path = posixpath.normpath(posixpath.join(Ctx.base, target.split("#")[0]))
    return "%s (раздел %d)" % (text, LINKS[path]) if path in LINKS else text


def plain(text: str) -> str:
    """Текст без разметки — для заголовков."""
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: link_text(m.group(1), m.group(2)), text)
    text = re.sub(r"(?<!\*)\*([^*\s][^*]*)\*(?!\*)", r"\1", text.replace("**", ""))
    return text.replace("`", "").strip()


# ---------- оформление ----------
def fonts(rpr, name: str) -> None:
    """Шрифт без ссылок на тему документа: у темы приоритет над явным шрифтом."""
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.insert(0, rf)
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        rf.attrib.pop(qn(attr), None)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rf.set(qn(attr), name)


def paragraph_style(doc, name, base="Normal"):
    try:
        return doc.styles[name]
    except KeyError:
        st = doc.styles.add_style(name, 1)
        st.base_style = doc.styles[base]
        return st


def setup(doc: Document) -> None:
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2)
    sec.top_margin, sec.bottom_margin = Cm(2), Cm(1.8)
    defaults = doc.styles.element.find(qn("w:docDefaults")).find(qn("w:rPrDefault")).find(qn("w:rPr"))
    fonts(defaults, FONT)
    st = doc.styles
    normal = st["Normal"]
    normal.font.size, normal.font.color.rgb = Pt(10.5), INK
    fonts(normal.element.get_or_add_rPr(), FONT)
    lang = OxmlElement("w:lang")                  # проверка орфографии Word — русская
    lang.set(qn("w:val"), "ru-RU")
    normal.element.get_or_add_rPr().append(lang)
    normal.paragraph_format.space_after, normal.paragraph_format.line_spacing = Pt(5), 1.12
    for name, size, color in (("Heading 1", 17, BLUE), ("Heading 2", 13.5, BLUE), ("Heading 3", 11.5, INK),
                              ("Heading 4", 10.5, INK)):
        s = st[name]
        s.font.size, s.font.bold, s.font.italic, s.font.color.rgb = Pt(size), True, False, color
        fonts(s.element.get_or_add_rPr(), FONT)
        s.paragraph_format.space_before, s.paragraph_format.space_after = Pt(size * 0.9), Pt(5)
        s.paragraph_format.keep_with_next = True
    st["Heading 1"].paragraph_format.page_break_before = True
    code = paragraph_style(doc, "Код")
    code.style_id = "CodeBlock"
    code.font.size = Pt(8.5)
    fonts(code.element.get_or_add_rPr(), MONO)
    code.paragraph_format.space_after, code.paragraph_format.line_spacing = Pt(0), 1.0
    code.paragraph_format.left_indent = Cm(0.3)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), CODE_FILL)
    code.element.get_or_add_pPr().append(shd)
    st["List Bullet"].paragraph_format.space_after = Pt(2)
    numbered = paragraph_style(doc, "Нумерованный пункт")
    numbered.style_id = "NumberedItem"
    numbered.paragraph_format.left_indent, numbered.paragraph_format.first_line_indent = Cm(0.7), Cm(-0.7)
    numbered.paragraph_format.space_after = Pt(2)
    for level, size, bold, indent in ((1, 10.5, True, 0), (2, 10, False, 0.6)):
        s = paragraph_style(doc, "toc %d" % level)   # встроенные стили оглавления Word
        s.font.size, s.font.bold = Pt(size), bold
        s.paragraph_format.left_indent = Cm(indent)
        s.paragraph_format.space_before, s.paragraph_format.space_after = Pt(5 if level == 1 else 0), Pt(1)


def field_begin(run, code: str) -> None:
    for kind, text in (("begin", None), (None, code), ("separate", None)):
        if kind:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), kind)
        else:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        run._r.append(el)


def field_end(run) -> None:
    el = OxmlElement("w:fldChar")
    el.set(qn("w:fldCharType"), "end")
    run._r.append(el)


def shade(cell, color: str) -> None:
    sh = OxmlElement("w:shd")
    sh.set(qn("w:val"), "clear")
    sh.set(qn("w:color"), "auto")
    sh.set(qn("w:fill"), color)
    cell._tc.get_or_add_tcPr().append(sh)


INLINE = re.compile(r"(\*\*.+?\*\*|(?<!\*)\*[^*\s][^*]*\*(?!\*)|`[^`]+`|\[[^\]]+\]\([^)]+\))")


def inline(paragraph, text: str, size=None, bold=False) -> None:
    """Абзац с разметкой: **жирный**, `код`, [ссылка](файл)."""
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            inline(paragraph, part[2:-2], size, True)
            continue
        if part.startswith("*") and part.endswith("*") and len(part) > 2 and not part.startswith("**"):
            before = len(paragraph.runs)
            inline(paragraph, part[1:-1], size, bold)
            for run in paragraph.runs[before:]:
                run.italic = True
            continue
        if part.startswith("`") and part.endswith("`") and len(part) > 2:
            run = paragraph.add_run(part[1:-1])
            run.font.name = MONO
            run.font.size = Pt((size or 10.5) - 1)
        elif part.startswith("[") and re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", part):
            m = re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", part)
            run = paragraph.add_run(link_text(m.group(1), m.group(2)))
            run.font.color.rgb = BLUE
        else:
            run = paragraph.add_run(part.replace("&lt;", "<").replace("&gt;", ">"))
        if size:
            run.font.size = Pt(size)
        if bold:
            run.bold = True


def column_widths(rows, size: float):
    """Ширины колонок: каждой — не уже самого длинного слова, остальное — по объёму текста."""
    cols, char = len(rows[0]), 0.0185 * size
    need = [min(WIDTH_CM / cols, 0.4 + char * max(len(w) for r in rows for w in (plain(r[j]).split() or [""])))
            for j in range(cols)]
    volume = [sum(len(plain(r[j])) for r in rows) ** 0.7 + 1 for j in range(cols)]
    rest = max(0.0, WIDTH_CM - sum(need))
    return [n + rest * v / sum(volume) for n, v in zip(need, volume)]


def table(doc: Document, rows) -> None:
    cols = max(len(r) for r in rows)
    rows = [r + [""] * (cols - len(r)) for r in rows]
    size = 8.5 if cols > 3 else 9
    widths = column_widths(rows, size)
    t = doc.add_table(rows=len(rows), cols=cols)
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    for j, w in enumerate(widths):
        t.columns[j].width = Cm(w)
    for i, r in enumerate(rows):
        for j, text in enumerate(r):
            cell = t.cell(i, j)
            cell.width = Cm(widths[j])
            p = cell.paragraphs[0]
            p.paragraph_format.space_after, p.paragraph_format.line_spacing = Pt(0), 1.0
            inline(p, text.strip(), size=size, bold=(i == 0))
            if i == 0:
                shade(cell, HEAD_FILL)
    trpr = t.rows[0]._tr.get_or_add_trPr()        # строка заголовка повторяется на каждой странице
    rh = OxmlElement("w:tblHeader")
    rh.set(qn("w:val"), "true")
    trpr.append(rh)
    doc.add_paragraph().paragraph_format.space_after = Pt(0)


LIST = re.compile(r"(-|\*|\d+\.) ")


def markdown(doc: Document, text: str, base: str = "") -> None:
    """Подмножество Markdown документов репозитория: заголовки, абзацы, списки, таблицы, код.
    Заголовок первого уровня (название файла) пропускается — его заменяет заголовок раздела."""
    Ctx.base = base
    lines, i, para = text.splitlines(), 0, []

    def flush():
        if para:
            inline(doc.add_paragraph(), " ".join(para))
            para.clear()

    while i < len(lines):
        s = lines[i].strip()
        image = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", s)
        if s.startswith("<!--") and s.endswith("-->"):
            flush()                               # метки генератора справочных разделов и служебные пометки
        elif image:
            flush()
            picture(doc, ROOT / posixpath.normpath(posixpath.join(base, image.group(2))), image.group(1))
        elif s.startswith("```"):
            flush()
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                doc.add_paragraph(lines[i].rstrip() or " ", style="Код")
                i += 1
            doc.add_paragraph().paragraph_format.space_after = Pt(0)
        elif s.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", lines[i].strip())[1:-1]]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            table(doc, rows)
            continue
        elif re.match(r"#{1,4} ", s):
            flush()
            level = len(s) - len(s.lstrip("#"))
            if level > 1:
                doc.add_heading(plain(s[level:]), min(4, level))
        elif LIST.match(s):
            flush()
            marker, item = s.split(" ", 1)
            while i + 1 < len(lines) and lines[i + 1].startswith("  ") and lines[i + 1].strip() \
                    and not LIST.match(lines[i + 1].strip()):
                i += 1
                item += " " + lines[i].strip()
            if marker[0].isdigit():
                p = doc.add_paragraph(style="Нумерованный пункт")
                p.add_run(marker + "\t")
                inline(p, item)
            else:
                inline(doc.add_paragraph(style="List Bullet"), item)
        elif not s:
            flush()
        else:
            para.append(s)
        i += 1
    flush()


def picture(doc: Document, path: Path, caption: str) -> None:
    """Рисунок во всю ширину текста и подпись «Рисунок N — …»."""
    doc.add_picture(str(path), width=Cm(16))
    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.paragraphs[-1].paragraph_format.keep_with_next = True
    Ctx.figures += 1
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(10)
    r = p.add_run("Рисунок %d — %s" % (Ctx.figures, caption))
    r.italic, r.font.size, r.font.color.rgb = True, Pt(9), MUTED


def title_page(doc: Document) -> None:
    for text, size, bold, space in (("ЛИДЕРЫ ЦИФРОВОЙ ТРАНСФОРМАЦИИ · ЗАДАЧА 9", 10, True, 110),
                                    ("Учебное программное обеспечение для подготовки оператора ДДС "
                                     "с использованием искусственного интеллекта", 20, True, 18),
                                    ("Тренажёр АРМ-112", 15, False, 6),
                                    ("Сопроводительная документация", 15, False, 170),
                                    ("Заказчик: Департамент ГОЧСиПБ города Москвы, ГБУ «Система 112»", 10.5, False, 4),
                                    ("Версия %s · %s" % (arm112.__version__, time.strftime("%d.%m.%Y")), 10.5, False, 4)):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(space)
        r = p.add_run(text)
        r.font.size, r.bold = Pt(size), bold
        if size >= 15:
            r.font.color.rgb = BLUE


def contents(doc: Document, anchor, entries) -> None:
    """Оглавление — поле TOC с готовым списком разделов: читается и без обновления полей
    (просмотрщики, LibreOffice); Word при открытии проставит номера страниц."""
    for n, (level, text) in enumerate(entries):
        p = anchor.insert_paragraph_before(style="toc %d" % level)
        if n == 0:
            field_begin(p.add_run(), 'TOC \\o "1-2" \\h \\z \\u')
        p.add_run(text)
    field_end(anchor.add_run())


def build(out: Path = OUT) -> dict:
    doc = Document()
    setup(doc)
    props = doc.core_properties
    props.title = "Тренажёр АРМ-112 — сопроводительная документация"
    props.subject = "Учебное ПО для подготовки оператора ДДС с использованием ИИ"
    props.language, props.author = "ru-RU", "Команда АРМ-112"
    title_page(doc)
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    r = doc.add_paragraph().add_run("Содержание")
    r.bold, r.font.size, r.font.color.rgb = True, Pt(17), BLUE
    anchor = doc.add_paragraph()
    sec = doc.sections[0]
    sec.different_first_page_header_footer = True
    hp = sec.header.paragraphs[0]
    run = hp.add_run("Тренажёр АРМ-112 · Сопроводительная документация")
    run.font.size, run.font.color.rgb = Pt(8.5), MUTED
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = fp.add_run()
    field_begin(run, "PAGE")
    field_end(run)
    parts = sections()
    Ctx.figures = 0
    for n, (title, text, src) in enumerate(parts, 1):
        doc.add_heading("%d. %s" % (n, title), 1)
        markdown(doc, text, posixpath.dirname(src) if src else "")
    entries = [(int(p.style.name[-1]), p.text) for p in doc.paragraphs if p.style.name in ("Heading 1", "Heading 2")]
    contents(doc, anchor, entries)
    upd = OxmlElement("w:updateFields")               # Word обновит оглавление и номера страниц при открытии
    upd.set(qn("w:val"), "true")
    doc.settings.element.append(upd)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out)
    return {"sections": len(parts), "headings": len(entries), "tables": len(doc.tables), "figures": Ctx.figures,
            "paragraphs": len(doc.paragraphs), "kb": round(out.stat().st_size / 1024)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    stats = build(a.out)
    print("%s: частей %d, заголовков в оглавлении %d, таблиц %d, рисунков %d, абзацев %d, %d КБ"
          % (a.out, stats["sections"], stats["headings"], stats["tables"], stats["figures"], stats["paragraphs"], stats["kb"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
