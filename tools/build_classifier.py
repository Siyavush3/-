# -*- coding: utf-8 -*-
"""Сборка data/classifier/classifier.json из xlsx-классификатора происшествий.

Шапка классификатора трёхуровневая:
  ряд 0 — служба («Классификатор МЧС», «ЦЭМП», «ОАТИ» …),
  ряд 1 — подразделение («Служба 101», «ОДС ПСЦ») ИЛИ условие (у МВД и СМП) ИЛИ канал
          («Дежурная служба АРМ-112», «МКП, Аналитика»),
  ряд 2 — условие маршрутизации («газификация», «угроза людям» …).
Условия приводятся к каноническим флагам, чтобы маршрутизацию можно было вычислять.

Запуск:  python tools/build_classifier.py <путь к xlsx> [data/classifier/classifier.json]
"""
import json
import os
import re
import sys

import openpyxl

HIDDEN_OPERATOR = "Не отображается оператору 112"

# Канонические флаги. Ключ — то, что пишется в шапке классификатора.
FLAG_WORDS = [
    (r"\bНД\b|НЕТ ДОСТУПА", "НД"),
    (r"\bУЛ\b|УГРОЗА ЛЮДЯМ|угроза людям", "УЛ"),
    (r"Пострадавшие не на месте", "ПП_НЕ_НА_МЕСТЕ"),
    (r"\bПП\b|ПОСТРАДАВШИЕ|Пострадавшие|пострадавшие|постр / погибшие", "ПП"),
    (r"Правонарушение", "ПРАВОНАРУШЕНИЕ"),
    (r"газификация", "ГАЗ"),
    (r"мед\. помощь", "МЕДПОМОЩЬ"),
    (r"Эвакуация", "ЭВАКУАЦИЯ"),
    (r">5 чел", "БОЛЕЕ_5_ЧЕЛ"),
    (r"перекрытие движение", "ПЕРЕКРЫТИЕ"),
    (r"^тоннель$", "ТОННЕЛЬ"),
    (r"^пеш$", "ПЕШ_ПЕРЕХОД"),
    (r"^ав$", "АВТО_ОБЪЕКТ"),
    (r"на объектах связи", "ОБЪЕКТ_СВЯЗИ"),
    (r"стройка", "СТРОЙКА"),
    (r"объект из перечня", "ОБЪЕКТ_ИЗ_ПЕРЕЧНЯ"),
]

# Отрицательные условия: «признак не выбран», «другие признаки не выбраны» и т.п.
NEGATIVE = re.compile(r"не выбран")
# Метки ряда 1 без ряда 2, которые означают канал доставки, а не условие.
CHANNELS = {
    "Дежурная служба АРМ-112": "АРМ-112",
    "МКП, Аналитика (Старый КРИМ)": "аналитика",
    "(КУБ)": "КУБ",
    "пожары": "пожары",
    "интеграция": "интеграция",
    "АРМ-112": "АРМ-112",
    "События по полигонам": "полигоны",
    "Москва": "Москва",
}
# Подразделения МЧС в ряду 1.
MCHS_UNITS = {"Служба 101", "ОДС ПСЦ", "МГПСС"}


def cell(v):
    return "" if v is None else " ".join(str(v).split())


def flags_in(text):
    found = []
    for pat, flag in FLAG_WORDS:
        if re.search(pat, text) and flag not in found:
            # «Пострадавшие не на месте» не должен дополнительно давать ПП
            if flag == "ПП" and "ПП_НЕ_НА_МЕСТЕ" in found and "не на месте" in text:
                continue
            found.append(flag)
    return found


def parse_header(h0, h1, h2, start):
    """Возвращает описание каждой колонки маршрутизации (индекс ≥ start)."""
    cols, service, unit = [], "", ""
    for i in range(start, len(h0)):
        if h0[i]:
            service, unit = h0[i], ""
        if h1[i] in MCHS_UNITS:
            unit = h1[i]
        raw = h2[i] or (h1[i] if h1[i] not in MCHS_UNITS else "")
        channel = CHANNELS.get(h1[i]) if not h2[i] else None
        cond_text = "" if channel else raw
        cols.append({"idx": i, "service": service, "unit": unit, "raw": cond_text,
                     "channel": channel})

    # Для отрицательного условия нужен список флагов, которые «не выбраны».
    # Если в тексте флаг назван явно — берём его, иначе — все флаги соседних колонок этой службы.
    by_service = {}
    for c in cols:
        by_service.setdefault(c["service"], []).append(c)
    for c in cols:
        text = c["raw"]
        if not text:
            c["cond"] = {}
        elif NEGATIVE.search(text):
            named = flags_in(text)
            if not named:
                named = sorted({f for s in by_service[c["service"]] if s is not c and s["raw"]
                                and not NEGATIVE.search(s["raw"]) for f in flags_in(s["raw"])})
            c["cond"] = {"none_of": named}
        elif text == "реагирование всегда":
            c["cond"] = {}
        else:
            c["cond"] = {"all_of": flags_in(text)}
            if not c["cond"]["all_of"]:
                c["cond"] = {"unparsed": text}
    return cols


def norm_value(v):
    s = cell(v)
    if s.replace(" ", "").lower() == "карточка-112":
        return "карточка-112"
    return s


def build(xlsx_path):
    ws = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True).worksheets[0]
    rows = [[cell(c) for c in r] for r in ws.iter_rows(values_only=True)]
    h0, h1, h2 = rows[0], rows[1], rows[2]
    width = max(len(h0), len(h1), len(h2))
    h0, h1, h2 = (r + [""] * (width - len(r)) for r in (h0, h1, h2))
    # Колонки ищем по заголовкам: в разных версиях они сдвинуты
    # (в v.046.11 перед «Главной службой» есть «Сценарий реагирования»).
    main_col = h0.index("Главная служба")
    scen_col = h0.index("Сценарий реагирования") if "Сценарий реагирования" in h0 else None
    cols = parse_header(h0, h1, h2, start=main_col + 1)

    body = rows[3:]
    # Код происшествия: Г (1–2 знака) + п1 + п2 + п3 (по 2 знака), т.е. 7 или 8 цифр.
    # Уровни отсчитываются с конца: у групп 10–24 номер группы двузначный.
    groups = {r[4]: {"code": r[4], "name": r[5]} for r in body
              if len(r) > 5 and r[4].isdigit() and len(r[4]) <= 2 and r[5]}
    incidents = []
    for r in body:
        if len(r) <= 4 or not (r[4].isdigit() and len(r[4]) >= 7):
            continue
        r = r + [""] * (width - len(r))
        code = r[4]
        group = code[:-6]
        if group not in groups:
            # У группы нет строки-заголовка (так у группы 24 «БПЛА») — берём имя из её строк.
            groups[group] = {"code": group, "name": r[5] or r[6], "name_inferred": True}
        routing = []
        for c in cols:
            val = norm_value(r[c["idx"]])
            if not val:
                continue
            entry = {"service": c["service"], "value": val}
            if c["unit"]:
                entry["unit"] = c["unit"]
            if c["cond"]:
                entry["cond"] = c["cond"]
            if c["channel"]:
                entry["channel"] = c["channel"]
            routing.append(entry)
        incidents.append({
            "code": code,
            "group": group,
            "p1": code[-6:-4], "p2": code[-4:-2], "p3": code[-2:],
            "stat_group": r[5],
            "operator_visible": bool(r[6]) and r[6] != HIDDEN_OPERATOR,
            "attr1": "" if r[6] == HIDDEN_OPERATOR else r[6],
            "attr2": r[7], "attr3": r[8],
            "extra_attrs": r[9],
            "type": r[10], "type_ekp35": r[11],
            "main_service": r[main_col],
            **({"scenario": r[scen_col]} if scen_col is not None and r[scen_col] else {}),
            "routing": routing,
        })

    m = re.search(r"v_(\d+)_(\d+)", os.path.basename(xlsx_path))
    return {
        "source": os.path.basename(xlsx_path),
        "version": "%s.%s" % m.groups() if m else "?",
        "flags": sorted({f for _, f in FLAG_WORDS}),
        "columns": [{k: v for k, v in c.items() if k != "raw" and v not in ("", None, {})}
                    for c in cols],
        "groups": sorted(groups.values(), key=lambda g: int(g["code"])),
        "incidents": sorted(incidents, key=lambda i: int(i["code"])),
    }


if __name__ == "__main__":
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else "data/classifier/classifier.json"
    out = build(src)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    unparsed = [c for c in out["columns"] if "unparsed" in c.get("cond", {})]
    print("версия %s: групп %d, происшествий %d, колонок маршрутизации %d, нераспознанных условий %d"
          % (out["version"], len(out["groups"]), len(out["incidents"]), len(out["columns"]), len(unparsed)))
