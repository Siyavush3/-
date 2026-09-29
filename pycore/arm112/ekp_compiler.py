# -*- coding: utf-8 -*-
"""Компилятор классификатора в таблицу маршрутизации ядра — classifier.ekp (формат v1).

Формат описан в core/src/ekp/FORMAT.md. Модуль совместим с Python 3.8: его вызывает сборка
ядра (tools/compile_ekp.py), в том числе в контейнере Ubuntu 20.04, где свежего Python нет.
Сервер пользуется им же, чтобы собрать таблицу при старте.
"""
import struct

MAGIC = b"A112EKP\0"
FORMAT_VERSION = 1
HEADER_SIZE = 64
MAX_FLAGS = 32
MAX_SERVICES = 256
MAX_TERRITORIAL = 32
NO_RESPONSE = "нет реагирования"

KIND_SERVICE, KIND_TERRITORIAL = 0, 1
COND_ALWAYS, COND_ALL_OF, COND_NONE_OF = 0, 1, 2


def fnv1a32(data):
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def _align8(buf):
    buf.extend(b"\0" * (-len(buf) % 8))


def build(classifier, ui):
    flags = list(classifier["flags"])
    if len(flags) > MAX_FLAGS:
        raise ValueError("флагов больше %d" % MAX_FLAGS)
    flag_bit = {f: i for i, f in enumerate(flags)}

    visible = ui["visible"]
    channel = {(c["service"], c["channel"]): c["ui"] for c in ui["channel_visible"]}
    territorial = ui["territorial"]

    services = []
    for name in list(visible.values()) + [c["ui"] for c in ui["channel_visible"]]:
        if name not in services:
            services.append(name)
    terr_names = []
    for name in territorial.values():
        if name not in terr_names:
            terr_names.append(name)
    if len(services) > MAX_SERVICES or len(terr_names) > MAX_TERRITORIAL:
        raise ValueError("слишком много служб или территориальных классов")
    service_idx = {n: i for i, n in enumerate(services)}
    terr_idx = {n: i for i, n in enumerate(terr_names)}

    def mask(names):
        m = 0
        for n in names:
            m |= 1 << flag_bit[n]
        return m

    incidents, entries = [], []
    for inc in sorted(classifier["incidents"], key=lambda i: int(i["code"])):
        first = len(entries)
        for r in inc["routing"]:
            if r["value"] == NO_RESPONSE:
                continue
            svc = r["service"]
            if svc in territorial:
                kind, target = KIND_TERRITORIAL, terr_idx[territorial[svc]]
            elif r.get("channel"):
                name = channel.get((svc, r["channel"]))
                if name is None:
                    continue
                kind, target = KIND_SERVICE, service_idx[name]
            elif svc in visible:
                kind, target = KIND_SERVICE, service_idx[visible[svc]]
            else:
                continue  # информационный получатель — на АРМ-112 не виден
            cond = r.get("cond") or {}
            if not cond:
                ck, m = COND_ALWAYS, 0
            elif "all_of" in cond:
                ck, m = COND_ALL_OF, mask(cond["all_of"])
            elif "none_of" in cond:
                ck, m = COND_NONE_OF, mask(cond["none_of"])
            else:
                raise ValueError("код %s: нераспознанное условие %r" % (inc["code"], cond))
            entries.append((target, kind, ck, m))
        incidents.append((int(inc["code"]), first, len(entries) - first))

    names = flags + services + terr_names + [classifier.get("version", "?")]
    strings = bytearray()
    name_offsets = []
    for n in names:
        name_offsets.append(len(strings))
        strings.extend(n.encode("utf-8") + b"\0")

    body = bytearray(HEADER_SIZE)
    off_names = len(body)
    for o in name_offsets:
        body.extend(struct.pack("<I", o))
    _align8(body)
    off_incidents = len(body)
    for code, first, count in incidents:
        body.extend(struct.pack("<III", code, first, count))
    _align8(body)
    off_entries = len(body)
    for target, kind, ck, m in entries:
        body.extend(struct.pack("<HBBI", target, kind, ck, m))
    _align8(body)
    off_strings = len(body)
    body.extend(strings)
    _align8(body)

    checksum = fnv1a32(bytes(body[HEADER_SIZE:]))
    header = struct.pack("<8s14I", MAGIC, FORMAT_VERSION, len(flags), len(services), len(terr_names),
                         len(incidents), len(entries), off_names, off_incidents, off_entries,
                         off_strings, len(strings), len(body), checksum, 0)
    assert len(header) == HEADER_SIZE
    body[:HEADER_SIZE] = header
    return bytes(body)
