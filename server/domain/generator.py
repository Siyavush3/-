"""Генерация учебных сценариев с эталоном (узел 4).

ТЗ, сценарий «Настройка учебной среды»: преподаватель выбирает категории событий, система генерирует
карточки и сама формирует «эталон» — правильные действия, с которыми сравнивается обучающийся;
преподаватель подтверждает эталон целиком или частично, видит вопросы с подсветкой правильных ответов
и может написать комментарий, по которому система исправляет карточку.

Генерация комбинаторная и детерминированная (тот же seed — тот же сценарий): коды — из классификатора,
адреса — из учебного справочника (data/gazetteer), обстоятельства — из шаблонов, эталон ДДС — из правил
памятки: чужая УК, чужой лифт, территория ГБУ «Автодороги», электрощитовые ОЭК, дубль, отказ после
принятия. Языковая модель (domain.llm) необязательна: она только перефразирует текст по комментарию,
который правила не распознали; эталон всегда за правилами.
"""
from __future__ import annotations

import copy
import json
import random
import re
from typing import Dict, List, Optional, Sequence, Tuple

from arm112.paths import data_dir
from arm112.reference.territory import Place

PASS_ON = ["передан", "сообщ", "направ", "звонил", "уведом"]
STEPS = ["Начало реагирования", "Прибытие", "Проведение работ", "Работы завершены"]
AFTER = [40, 95, 150, 240]
BRIGADES = {
    "water": ["Выехала аварийная бригада {org}, два сантехника", "Бригада на месте, перекрываем стояк",
              "Меняем участок трубы, откачиваем воду", "Течь устранена, водоснабжение восстановлено"],
    "electric": ["Направлен дежурный электрик {org}", "Электрик на месте, проверяет этажный щиток",
                 "Заменён автомат, проверяем линию", "Освещение восстановлено, работы завершены"],
    "roof": ["Выехала бригада кровельщиков {org}", "Бригада на месте, огородили опасную зону",
             "Сбиваем наледь и закрепляем элементы", "Опасность устранена, ограждение снято"],
    "lift": ["Сообщили в «Мослифт», выехал механик", "Механик на месте, работает с лифтом",
             "Лифт остановлен, идёт диагностика", "Лифт исправен и запущен"],
    "yard": ["Выехала бригада благоустройства {org}", "Бригада на месте, огородили участок",
             "Работы ведутся, техника на месте", "Работы завершены, проход свободен"],
    "fire": ["Направлены сантехник и дежурный электрик участка", "Сантехник и электрик на месте, пожарные работают",
             "По требованию пожарных отключены стояк и электроснабжение подъезда",
             "Работы завершены, подъезд обесточен до осмотра"],
    "chute": ["Направлен рабочий по обслуживанию мусоропровода", "Рабочий на месте, пожарные работают",
              "Ствол мусоропровода очищен и промыт", "Работы завершены"],
    "generic": ["Выехала бригада {org}", "Бригада на месте, выясняет обстоятельства",
                "Работы ведутся", "Работы завершены"],
}
FAMILIES: Dict[str, dict] = {
    "water": {"group": "14", "codes": {
        "14020300": ["Течёт вода с потолка на кухне, квартира {flat}",
                     "Прорвало трубу горячей воды в подъезде {entrance}, заливает лестницу",
                     "В подвале дома вода по щиколотку, прорвало трубу"],
        "14040800": ["В квартире {flat} с утра нет холодной воды, у соседей есть"],
        "14040100": ["Из крана течёт ржавая вода, квартира {flat}, второй день"]}},
    "electric": {"group": "14", "codes": {
        "14030201": ["Нет света в подъезде {entrance}, на лестнице темно",
                     "В квартире {flat} пропал свет, у соседей свет есть"]}},
    "roof": {"group": "14", "codes": {
        "14090104": ["С крыши над подъездом {entrance} свисают большие сосульки"],
        "14090404": ["Над подъездом {entrance} отходит кусок облицовки балкона, может упасть"]}},
    "lift": {"group": "14", "codes": {
        "14100100": ["Застрял лифт в подъезде {entrance}, внутри женщина с ребёнком"],
        "14100400": ["Второй день не работает лифт в подъезде {entrance}, дом двенадцать этажей"]}},
    "yard": {"group": "14", "codes": {
        "14080106": ["Во дворе упало дерево, перегородило проход к подъезду {entrance}"],
        "14060306": ["Во дворе у подъезда {entrance} провалился асфальт, яма около метра"]}},
    "fire": {"group": "1", "flags": ["УЛ"], "chp": True, "codes": {
        "1050101": ["Горит квартира на {floor} этаже, из окна идёт дым, соседи выходят на улицу"]}},
    "chute": {"group": "1", "codes": {
        "1050602": ["Из мусоропровода в подъезде {entrance} идёт дым, открытого огня не видно"]}},
}
OEK_CODES = {"14050300": "В электрощитовой подъезда {entrance} гудит и пахнет гарью",
             "14050100": "В подъезде {entrance} сломана дверь электрощитовой, дверь открыта"}
REFUSAL = ("14110301", "Во дворе оборван провод, висит над тротуаром у подъезда {entrance}")
UK_NAMES = [("УК «Уютный дом»", "уютный"), ("УК «Домовой»", "домовой"), ("УК «Надёжный дом»", "надежный")]
LIFT_ORGS = [("ООО «ЛифтСервис»", "лифтсервис"), ("ООО «Подъём»", "подъем"), ("ООО «Практика»", "практик")]
KIND_TITLE = {"profile": "профильная", "other_uk": "дом другой УК", "lift_org": "лифт другой организации",
              "road": "территория ГБУ «Автодороги»", "oek": "электрощитовая ОЭК", "duplicate": "дубль",
              "refusal": "отказ после принятия", "student": "карточка обучающегося"}
TRAPS_AT = {1: ["duplicate", "other_uk"],
            2: ["other_uk", "lift_org", "road", "oek", "duplicate"],
            3: ["other_uk", "lift_org", "road", "oek", "duplicate", "refusal"]}
LEVELS = {1: {"share": 0.3, "interval": 60}, 2: {"share": 0.5, "interval": 60}, 3: {"share": 0.6, "interval": 45}}
LEVEL_TITLE = {1: "базовая", 2: "средняя", 3: "высокая"}

# ---------- вызовы оператора 112 ----------
CAR = ["«Фольксваген Поло»", "«Киа Рио»", "«Лада Веста»", "«Тойота Камри»", "«Хендай Солярис»"]
COLOR = ["белый", "чёрный", "серый", "красный", "синий"]


def _t(code, say, short, facts, flags=(), victims=False, indoor=False, status="очевидец", fixed=False):
    return {"code": code, "say": say, "short": short, "facts": facts, "flags": list(flags), "victims": victims,
            "indoor": indoor, "status": status, "victims_fixed": fixed}


CALLS: Dict[str, List[dict]] = {
    "1": [_t("1050101", "Горит квартира на {floor} этаже, из окна валит дым, соседи выбегают на улицу",
             "Помогите, у нас пожар!", [["горит", "пожар", "пламя"], ["квартир"], ["дым"]],
             flags=["УЛ"], victims=None, indoor=True),
          _t("1050602", "В подъезде дымит мусоропровод, дышать нечем, огня не видно",
             "У нас в подъезде дым, дышать нечем!", [["мусоропровод"], ["дым", "задымл"]], indoor=True),
          _t("1020201", "Во дворе горит машина, {car}, {color}, людей рядом нет",
             "Тут машина горит!", [["машин", "автомоб", "а/м"], ["горит", "пожар"]])],
    "2": [_t("2010100", "Столкнулись две машины, {car} и {car2}, никто не пострадал",
             "Тут авария, машины разбиты!", [["столкн", "дтп", "авари"],
                                              ["без пострадав", "не пострадал", "пострадавших нет"]], fixed=True),
          _t("2020100", "Сбили пешехода на переходе, мужчина лежит на дороге и не встаёт",
             "Человека сбили, скорее!", [["сбил", "наезд", "пешеход"], ["лежит", "без сознан", "не вста"]],
             flags=["ПП"], victims=True, fixed=True)],
    "13": [_t("13020100", "На кухне сильно пахнет газом, плита выключена",
              "У нас газом пахнет, страшно!", [["газ"], ["кухн"]], flags=["ГАЗ"], indoor=True),
           _t("13010400", "На улице возле подъезда {entrance} сильно пахнет газом",
              "Газом пахнет на улице!", [["газ"], ["улиц", "подъезд"]])],
    "14": [_t("14020300", "Прорвало трубу, вода течёт с потолка по стене",
              "Нас заливает, вода отовсюду!", [["вод", "течь", "трубу", "прорв"], ["потол", "стен"]], indoor=True),
           _t("14100100", "Застрял в лифте, подъезд {entrance}, лифт стоит между этажами",
              "Я застрял, помогите!", [["лифт"], ["застрял", "застрев"]], indoor=True)],
    "15": [_t("15060100", "У соседей драка, кричат, что-то бьётся, квартира {flat}",
              "У соседей такое творится, приезжайте!", [["драк", "дерут"], ["сосед", "квартир"]],
              flags=["ПРАВОНАРУШЕНИЕ"], indoor=True),
           _t("15210101", "Только что угнали мою машину, {car}, {color}, уехали в сторону центра",
              "У меня машину угнали!", [["угнал", "угон"], ["машин", "автомоб"]],
              flags=["ПРАВОНАРУШЕНИЕ"], status="пострадавший")],
    "17": [_t("17070200", "Человек тонет в пруду, кричит, я не умею плавать",
              "Там человек в воде, скорее!", [["тонет", "тонущ"], ["пруд", "вод"]],
              flags=["УЛ"], victims=True, fixed=True),
           _t("17040100", "На стройке рабочий упал с высоты, лежит и не двигается",
              "Человек упал, не двигается!", [["упал", "падени"], ["высот"], ["строй", "рабоч"]],
              flags=["ПП"], victims=True, fixed=True)],
    "22": [_t("22370000", "Мужу плохо с сердцем, давит в груди, ему шестьдесят лет",
              "Мужу плохо, помогите!", [["сердц", "груд"]], flags=["ПП"], victims=True, indoor=True,
              status="родственник", fixed=True),
           _t("22170000", "Ребёнок задыхается, приступ астмы, ингалятор не помогает",
              "Ребёнок не может дышать!", [["задыха", "дыш"], ["астм"]], flags=["ПП"], victims=True,
              indoor=True, status="родственник", fixed=True)],
}
STATUS_SPEECH = {"очевидец": "Я просто рядом, всё вижу сам.", "пострадавший": "Это со мной случилось.",
                 "родственник": "Я родственник, звоню за него."}
VICTIMS_SPEECH = {True: "Да, есть пострадавшие.", False: "Пострадавших нет.", None: "Не знаю, не вижу."}


class GeneratorError(ValueError):
    pass


class Gazetteer:
    def __init__(self, raw: dict):
        self.raw = raw
        self.districts: Dict[str, dict] = raw["districts"]

    @classmethod
    def load(cls, path=None) -> "Gazetteer":
        with open(path or data_dir() / "gazetteer" / "moscow.json", encoding="utf-8") as f:
            return cls(json.load(f))

    def person(self, rng: random.Random) -> Tuple[str, str]:
        """«Фамилия Имя» и пол заявителя."""
        surname = rng.choice(self.raw["surnames_m"])
        if rng.random() < 0.5:
            return surname + "а " + rng.choice(self.raw["names_f"]), "f"
        return surname + " " + rng.choice(self.raw["names_m"]), "m"

    def words(self) -> List[str]:
        """Слова справочника — чтобы проверка орфографии не считала улицы и фамилии ошибками."""
        out = []
        for d in self.districts.values():
            out += d["streets"]
        out += list(self.districts)
        out += [s + "а" for s in self.raw["surnames_m"]] + self.raw["surnames_m"]
        return out + self.raw["names_m"] + self.raw["names_f"]


def vocabulary(gazetteer: Gazetteer) -> List[str]:
    """Слова генератора для словаря проверки орфографии: улицы, фамилии, учебные организации."""
    return gazetteer.words() + [n for n, _ in UK_NAMES + LIFT_ORGS] + ["Ростелеком", "Ростелекома"]


def short_street(street: str) -> str:
    """«улица Рогова» и «Пехотная улица» → «ул. Рогова», «ул. Пехотная» — как в справке службы."""
    if street.startswith("улица "):
        return "ул. " + street[6:]
    if street.endswith(" улица"):
        return "ул. " + street[:-6]
    return street


def _addr_text(addr: Sequence[str]) -> str:
    return "%s, д. %s" % (short_street(addr[0]), addr[1])


def _phone(rng: random.Random) -> str:
    return "+7999%07d" % rng.randint(0, 9_999_999)


def _slots(rng: random.Random) -> dict:
    cars = rng.sample(CAR, 2)
    return {"flat": rng.randint(1, 220), "entrance": rng.randint(1, 6), "floor": rng.randint(2, 16),
            "car": cars[0], "car2": cars[1], "color": rng.choice(COLOR)}


def _brigade(family: str, org: str) -> List[dict]:
    return [{"after": a, "status": s, "text": t.format(org=org)} for a, s, t in zip(AFTER, STEPS, BRIGADES[family])]


def _key(i: int) -> str:
    return chr(ord("A") + i) if i < 26 else "K%d" % i


def _slug(text: str) -> str:
    table = str.maketrans("абвгдеёжзийклмнопрстуфхцчшщъыьэюя", "abvgdeejziiklmnoprstufhccss_y_eua")
    return re.sub(r"[^a-z0-9]+", "-", text.lower().translate(table)).strip("-")


def family_of(code: str) -> Optional[str]:
    return next((f for f, v in FAMILIES.items() if code in v["codes"]), None)


def dds_families(groups: Optional[Sequence[str]]) -> List[str]:
    out = [f for f, v in FAMILIES.items() if not groups or v["group"] in groups]
    if not out:
        raise GeneratorError("для выбранных категорий нет шаблонов карточек ДДС: доступны группы 1 и 14")
    return out


def dds_groups() -> List[str]:
    return sorted({v["group"] for v in FAMILIES.values()}, key=int)


# ---------- ДДС: карточки с эталоном ----------
class DdsBuilder:
    """Всё, что нужно для карточек одного района: справка службы и правила эталона.
    Контекст (ctx) сериализуется в пакет — по нему же карточку потом правят по комментарию."""

    def __init__(self, gazetteer: Gazetteer, ctx: dict, rng: random.Random):
        self.g, self.ctx, self.rng = gazetteer, ctx, rng
        self.families = dds_families(ctx.get("groups"))

    @classmethod
    def new(cls, gazetteer: Gazetteer, district: str, groups: Optional[Sequence[str]], rng: random.Random):
        okrug = gazetteer.districts[district]["okrug"]
        streets = list(gazetteer.districts[district]["streets"])
        used: set = set()

        def addr():
            for _ in range(500):
                a = (rng.choice(streets), str(rng.randint(1, 45)))
                if a not in used:
                    used.add(a)
                    return list(a)
            raise GeneratorError("в справочнике не хватает адресов района %s" % district)

        def phone():
            return "+7 (999) 000-%02d-%02d" % (rng.randint(10, 99), rng.randint(10, 99))

        uk, lift = rng.choice(UK_NAMES), rng.choice(LIFT_ORGS)
        ctx = {"district": district, "okrug": okrug, "groups": list(groups or []),
               "org": "ГБУ «Жилищник района %s»" % district, "own": [addr() for _ in range(6)],
               "uk": [uk[0], uk[1], addr(), phone()], "lift": [lift[0], lift[1], addr(), phone()],
               "road": addr(), "oek_site": rng.randint(1, 12)}
        return cls(gazetteer, ctx, rng)

    def reference(self) -> dict:
        c = self.ctx
        return {"title": "Справка службы: ДДС района %s · %s" % (c["district"], c["org"]), "sections": [
            {"title": "Жилой фонд %s" % c["org"], "items": [_addr_text(a) for a in c["own"]]},
            {"title": "Дома под управлением других организаций",
             "items": ["%s — %s, диспетчерская %s" % (_addr_text(c["uk"][2]), c["uk"][0], c["uk"][3])]},
            {"title": "Лифты", "items": ["в домах %s — «Мослифт»" % c["org"],
                                         "исключение: %s — %s, диспетчерская %s"
                                         % (_addr_text(c["lift"][2]), c["lift"][0], c["lift"][3])]},
            {"title": "Дворовые территории и проезды",
             "items": ["%s — проезд и дворовая территория в ведении ГБУ «Автомобильные дороги %s»"
                       % (_addr_text(c["road"]), c["okrug"])]},
            {"title": "Электроснабжение и связь",
             "items": ["электрощитовые жилых домов — ОЭК, эксплуатационный участок № %d" % c["oek_site"],
                       "слаботочные сети (телефон, интернет) — операторы связи"]},
            {"title": "Порядок (памятка ДДС)", "items": [
                "первичный статус «Принята» / «Не принята» — в течение 30 секунд",
                "«Не принята» и «Отказ от выполнения работ» — всегда с комментарием: причина и куда передана информация",
                "статусы хода работ — по факту доклада старшего бригады, с комментарием"]},
        ]}

    def can(self, kind: str) -> bool:
        fam = set(self.families)
        return {"profile": True, "duplicate": True, "other_uk": bool(fam & {"water", "electric", "roof"}),
                "lift_org": "lift" in fam, "road": "yard" in fam, "oek": "electric" in fam,
                "refusal": bool(fam & {"electric", "yard"})}.get(kind, False)

    def card(self, kind: str, key: str, profiles: Sequence[dict]) -> dict:
        rng, c = self.rng, self.ctx
        person, _ = self.g.person(rng)
        slots = _slots(rng)
        base = {"key": key, "flags": [], "caller": {"name": person.split()[0], "status": "очевидец", "phone": _phone(rng)}}

        def place(addr, entrance=True):
            out = {"street": addr[0], "house": addr[1]}
            if entrance:
                out["entrance"] = str(slots["entrance"])
            return out

        def pick(families):
            fam = rng.choice(families)
            code, texts = rng.choice(sorted(FAMILIES[fam]["codes"].items()))
            return fam, code, rng.choice(texts).format(**slots)

        if kind == "profile":
            fam, code, text = pick(self.families)
            addr = rng.choice(c["own"])
            card = dict(base, code=code, flags=list(FAMILIES[fam].get("flags", [])), address=place(addr),
                        description=text, brigade=_brigade(fam, c["org"]),
                        expect={"primary": "Принята", "final": "Работы завершены",
                                "why": "Дом %s — в жилом фонде %s: «Принята» за 30 секунд, затем статусы хода работ "
                                       "по докладам бригады с комментариями, итог «Работы завершены»."
                                       % (_addr_text(addr), c["org"])},
                        gen={"kind": kind, "family": fam})
            if FAMILIES[fam].get("chp"):
                card["chp"] = True
            return card
        if kind == "other_uk":
            fam, code, text = pick([f for f in self.families if f in ("water", "electric", "roof")])
            uk, kw, addr, phone = c["uk"]
            return dict(base, code=code, address=place(addr), description=text,
                        expect={"primary": "Не принята", "comment_facts": [[kw], PASS_ON],
                                "why": "Дом %s обслуживает %s (справка службы): «Не принята», в комментарии — кто "
                                       "обслуживает дом и что информация передана." % (_addr_text(addr), uk),
                                "sample_comment": "Дом обслуживает %s, информация передана в их диспетчерскую %s."
                                                  % (uk, phone)},
                        gen={"kind": kind, "family": fam})
        if kind == "lift_org":
            fam, code, text = pick(["lift"])
            org, kw, addr, phone = c["lift"]
            return dict(base, code=code, address=place(addr), description=text,
                        expect={"primary": "Не принята", "comment_facts": [[kw], PASS_ON],
                                "why": "Лифт в доме %s обслуживает %s, а не «Мослифт» (справка службы): «Не принята», "
                                       "в комментарии — организация и что информация передана." % (_addr_text(addr), org),
                                "sample_comment": "Лифт обслуживает %s, информация передана в их диспетчерскую %s."
                                                  % (org, phone)},
                        gen={"kind": kind, "family": fam})
        if kind == "road":
            fam, code, text = pick(["yard"])
            return dict(base, code=code, address=place(c["road"], entrance=False), description=text,
                        expect={"primary": "Не принята",
                                "comment_facts": [["не обслуж", "ведени", "автодорог", "гбу ад", "ад " + c["okrug"].lower()]],
                                "why": "Проезд и двор у дома %s — в ведении ГБУ «Автомобильные дороги %s»: «Не принята» "
                                       "с указанием, в чьём ведении территория." % (_addr_text(c["road"]), c["okrug"]),
                                "sample_comment": "Территория в ведении ГБУ «Автомобильные дороги %s», "
                                                  "информация передана им." % c["okrug"]},
                        gen={"kind": kind, "family": fam})
        if kind == "oek":
            code, text = rng.choice(sorted(OEK_CODES.items()))
            return dict(base, code=code, address=place(rng.choice(c["own"])), description=text.format(**slots),
                        expect={"primary": "Не принята", "comment_facts": [["оэк", "участ"], PASS_ON],
                                "why": "Электрощитовые жилых домов — зона ОЭК (участок № %d): «Не принята», в "
                                       "комментарии — ОЭК и что информация передана." % c["oek_site"],
                                "sample_comment": "Электрощитовая в ведении ОЭК, информация передана на "
                                                  "эксплуатационный участок № %d." % c["oek_site"]},
                        gen={"kind": kind, "family": "electric"})
        if kind == "duplicate":
            if not profiles:
                raise GeneratorError("дублю нужна профильная карточка раньше него")
            src = rng.choice(list(profiles))
            text = src["description"]
            return dict(base, code=src["code"], flags=list(src["flags"]), address=dict(src["address"]),
                        description="Повторно сообщают: " + text[0].lower() + text[1:],
                        expect={"primary": "Не принята",
                                "comment_facts": [["дубл", "повтор", "по карточке", "{%s}" % src["key"]]],
                                "why": "То же происшествие, что в карточке %s, по нему уже работают: «Не принята» как "
                                       "дубль со ссылкой на номер первой карточки." % src["key"],
                                "sample_comment": "Дубль карточки {%s}, работы уже ведутся." % src["key"]},
                        gen={"kind": kind, "family": src["gen"]["family"], "of": src["key"]})
        if kind == "refusal":
            code, text = REFUSAL
            return dict(base, code=code, address=place(rng.choice(c["own"])), description=text.format(**slots),
                        expect={"primary": "Принята", "final": "Отказ от выполнения работ",
                                "final_comment_facts": [["ростелеком"], PASS_ON],
                                "why": "Провод во дворе своего дома — «Принята». После доклада бригады, что оборван "
                                       "кабель Ростелекома, — «Отказ от выполнения работ» с комментарием, кому передано.",
                                "sample_final_comment": "Оборван кабель Ростелекома, это не наша сеть, информация "
                                                        "передана в Ростелеком."},
                        brigade=[{"after": 50, "status": "Начало реагирования", "text": "Выехал дежурный электрик участка"},
                                 {"after": 110, "status": "Отказ от выполнения работ",
                                  "text": "На месте выяснилось: оборван телефонный кабель Ростелекома, это не наш провод"}],
                        gen={"kind": kind, "family": "electric"})
        raise GeneratorError("неизвестный вид карточки: %s" % kind)


def _check_route(router, raw: dict, card: dict) -> bool:
    place = Place(raw["place"]["district"], raw["place"]["okrug"])
    return raw["role"] in {p.name for p in router.plaques(card["code"], card.get("flags", []), place,
                                                          card.get("manual_services", []))}


def _retime(raw: dict) -> None:
    interval = LEVELS.get((raw.get("generated") or {}).get("difficulty", 1), LEVELS[1])["interval"]
    for i, card in enumerate(raw["cards"]):
        card["at"] = 10 + interval * i


def generate_dds(router, gazetteer: Gazetteer, district: str, difficulty: int = 1, count: int = 10,
                 groups: Optional[Sequence[str]] = None, seed: Optional[int] = None) -> dict:
    """Пакет «действия с карточками» для ДДС района — в формате data/scenarios/dds_*.json."""
    if district not in gazetteer.districts:
        raise GeneratorError("нет района «%s» в учебном справочнике" % district)
    if difficulty not in LEVELS:
        raise GeneratorError("сложность — 1, 2 или 3")
    if not 3 <= count <= 30:
        raise GeneratorError("карточек — от 3 до 30")
    seed = random.randrange(1_000_000) if seed is None else seed
    rng = random.Random("dds:%s:%s:%s:%s" % (district, difficulty, ",".join(groups or []), seed))
    role = router.territory.district_service(district)
    if role is None:
        raise GeneratorError("нет ДДС района «%s» в справочнике служб" % district)
    builder = DdsBuilder.new(gazetteer, district, groups, rng)
    traps = [k for k in TRAPS_AT[difficulty] if builder.can(k)]
    kinds = ["profile"] + [rng.choice(traps) if traps and rng.random() < LEVELS[difficulty]["share"] else "profile"
                           for _ in range(count - 1)]
    if difficulty >= 2 and traps and kinds.count("profile") == count:
        kinds[-1] = rng.choice(traps)
    if difficulty == 3 and "refusal" in traps and "refusal" not in kinds:
        kinds[rng.randrange(1, count)] = "refusal"
    raw = {"id": "gen-dds-%s-%d" % (_slug(district), seed),
           "title": "ДДС района %s: сгенерировано, сложность %s" % (district, LEVEL_TITLE[difficulty]),
           "source": "Генератор сценариев (узел 4): коды — классификатор, адреса — учебный справочник, эталон — "
                     "правила памятки ДДС.",
           "role": role, "role_title": "ДДС района " + district,
           "place": {"district": district, "okrug": builder.ctx["okrug"]}, "reference": builder.reference(),
           "cards": [], "generated": dict(builder.ctx, difficulty=difficulty, seed=seed)}
    profiles: List[dict] = []
    for i, kind in enumerate(kinds):
        for _ in range(20):
            card = builder.card(kind, _key(i), profiles)
            if _check_route(router, raw, card):
                break
        else:
            raise GeneratorError("не удалось подобрать карточку, доходящую до «%s»" % role)
        card["source"] = "генератор: " + KIND_TITLE[kind]
        raw["cards"].append(card)
        if kind == "profile":
            profiles.append(card)
    _retime(raw)
    return raw


# ---------- оператор 112: вызовы с эталоном карточки ----------
def _make_call(rng: random.Random, tpl: dict, group: str, gazetteer: Gazetteer, key: str, difficulty: int,
               person: Optional[Tuple[str, str]] = None, where: Optional[Tuple[str, str, str]] = None,
               slots: Optional[dict] = None) -> dict:
    slots = slots or _slots(rng)
    if where is None:
        district = rng.choice(sorted(gazetteer.districts))
        where = (district, rng.choice(gazetteer.districts[district]["streets"]), str(rng.randint(1, 45)))
    district, street, house = where
    name, gender = person or gazetteer.person(rng)
    phone = _phone(rng)
    say = tpl["say"].format(**slots)
    address = {"subject": "Москва", "street": street, "house": house}
    precise = "Москва, %s, дом %s" % (street, house)
    if tpl["indoor"]:
        address.update(entrance=str(slots["entrance"]), flat=str(slots["flat"]))
        precise += ", подъезд %s, квартира %s" % (slots["entrance"], slots["flat"])
    landmark = "Мы в районе %s, возле магазина, точный адрес не помню." % district
    flags = list(tpl["flags"])
    return {
        "key": key,
        "situation": "%s, %s, %s" % (say, name, phone),
        "caller": {"name": name, "phone": phone, "gender": gender},
        "gold": {"codes": [tpl["code"]], "flags": flags, "victims": tpl["victims"], "statuses": [tpl["status"]],
                 "name": name, "address": address, "facts": copy.deepcopy(tpl["facts"])},
        "dialogue": {
            "opening": "Алло! " + tpl["short"] if difficulty >= 3 else "Алло, 112? " + say + ".",
            "address": landmark if difficulty >= 2 else precise + ".",
            "address_clarified": precise + ".",
            "name": "Меня зовут %s." % name,
            "phone": "Мой номер — %s." % phone,
            "victims": VICTIMS_SPEECH[tpl["victims"]],
            "status": STATUS_SPEECH[tpl["status"]],
            "details": say + ".",
            "danger": "Да, людям угрожает опасность!" if "УЛ" in flags else "Угрозы людям вроде нет.",
            "gas": "Да, дом газифицирован." if "ГАЗ" in flags else "Не знаю.",
            "access": "Доступ есть.",
        },
        "gen": {"group": group, "code": tpl["code"], "difficulty": difficulty, "district": district,
                "slots": slots},
    }


def call_groups() -> List[str]:
    return sorted(CALLS, key=int)


def generate_calls(gazetteer: Gazetteer, groups: Optional[Sequence[str]] = None, difficulty: int = 1,
                   count: int = 8, seed: Optional[int] = None) -> dict:
    """Пакет «карточки» (оператор 112) — в формате data/scenarios/calls_bilety.json."""
    pool = [(g, t) for g in call_groups() if not groups or g in groups for t in CALLS[g]]
    if not pool:
        raise GeneratorError("для выбранных категорий нет шаблонов вызовов: доступны группы %s" % ", ".join(call_groups()))
    if difficulty not in LEVELS:
        raise GeneratorError("сложность — 1, 2 или 3")
    if not 1 <= count <= 50:
        raise GeneratorError("вызовов — от 1 до 50")
    seed = random.randrange(1_000_000) if seed is None else seed
    rng = random.Random("calls:%s:%s:%s" % (difficulty, ",".join(groups or []), seed))
    order = pool[:]
    rng.shuffle(order)
    calls = []
    for i in range(count):
        group, tpl = order[i] if i < len(order) else rng.choice(pool)
        calls.append(_make_call(rng, tpl, group, gazetteer, "g%02d" % (i + 1), difficulty))
    return {"id": "gen-calls-%d" % seed, "mode": "cards",
            "title": "Вызовы 112: сгенерировано, сложность %s" % LEVEL_TITLE[difficulty],
            "source": "Генератор сценариев (узел 4): шаблоны по группам классификатора, адреса — учебный справочник.",
            "norm_s": 80, "answer_norm_s": 15, "calls": calls,
            "generated": {"groups": list(groups or []), "difficulty": difficulty, "seed": seed}}


# ---------- правка по комментарию преподавателя ----------
DIRECTIVES = [
    ("harder", ("сложн", "ловушк", "не принят", "отказ", "запутан")),
    ("easier", ("прощ", "легч", "должна быть принят", "профильн")),
    ("address", ("адрес", "улиц", "другой дом")),
    ("type", ("другое происшеств", "другой тип", "другой код", "другую ситуац", "другой случай")),
    ("shorter", ("короч", "кратк", "сократ")),
    ("longer", ("подробн", "длинн", "добавь детал")),
    ("no_victims", ("без пострадав", "убери пострадав", "пострадавших нет")),
    ("victims", ("с пострадав", "есть пострадав", "добавь пострадав")),
]


def parse_comment(comment: str) -> List[str]:
    text = comment.lower().replace("ё", "е")
    found = [name for name, words in DIRECTIVES if any(w in text for w in words)]
    if "no_victims" in found and "victims" in found:
        found.remove("victims")
    if "harder" in found and "easier" in found:
        found.remove("harder" if ("прощ" in text or "легч" in text) else "easier")
    return found


def _find(items: List[dict], key: str) -> int:
    for i, c in enumerate(items):
        if c["key"] == key:
            return i
    raise GeneratorError("нет карточки %s" % key)


def _text_holder(pack: dict, key: str) -> Tuple[dict, str]:
    """Где лежит текст, который может переписать языковая модель: описание карточки ДДС или первая реплика."""
    if pack.get("mode") == "cards":
        return pack["calls"][_find(pack["calls"], key)]["dialogue"], "opening"
    return pack["cards"][_find(pack["cards"], key)], "description"


def item_text(pack: dict, key: str) -> str:
    holder, field_ = _text_holder(pack, key)
    return holder[field_]


def revise_rules(pack: dict, key: str, comment: str, router, gazetteer: Gazetteer) -> Tuple[dict, List[str]]:
    """Шаг 1 правки: что распознали правила. Пустой список изменений — правила комментарий не поняли."""
    comment = (comment or "").strip()
    if not comment:
        raise GeneratorError("напишите комментарий: что изменить в карточке")
    pack = copy.deepcopy(pack)
    if pack.get("mode") == "cards":
        return pack, _revise_call(pack, key, comment, gazetteer)
    return pack, _revise_dds(pack, key, comment, router, gazetteer)


def revise_text(pack: dict, key: str, text: Optional[str], checker=None) -> Tuple[dict, List[str]]:
    """Шаг 2: текст от языковой модели — только если он без орфографических ошибок."""
    if not text:
        return pack, []
    if checker is not None and checker.spelling(text):
        return pack, ["языковая модель ответила текстом с ошибками — текст оставлен прежним"]
    pack = copy.deepcopy(pack)
    holder, field_ = _text_holder(pack, key)
    holder[field_] = text
    return pack, ["текст переписан языковой моделью по комментарию"]


def finish_revision(pack: dict, key: str, comment: str, changes: List[str]) -> Tuple[dict, List[str]]:
    """Шаг 3: история правок в карточке — преподаватель видит, что и почему изменилось."""
    changes = list(changes) or ["комментарий не распознан правилами — карточка оставлена как есть, комментарий сохранён"]
    items = pack["calls"] if pack.get("mode") == "cards" else pack["cards"]
    items[_find(items, key)].setdefault("history", []).append({"comment": comment.strip(), "changes": changes})
    return pack, changes


def revise(pack: dict, key: str, comment: str, router, gazetteer: Gazetteer, llm=None,
           checker=None) -> Tuple[dict, List[str]]:
    """Исправить одну карточку (вызов) по комментарию: правила, затем (если есть) языковая модель."""
    pack, changes = revise_rules(pack, key, comment, router, gazetteer)
    if not changes and llm is not None and llm.available():
        pack, changes = revise_text(pack, key, llm.rewrite(item_text(pack, key), comment), checker)
    return finish_revision(pack, key, comment, changes)


def _referenced(pack: dict, key: str) -> List[str]:
    return [c["key"] for c in pack.get("cards", []) if c["key"] != key
            and any("{%s}" % key in v for g in (c.get("expect") or {}).get("comment_facts", []) for v in g)]


def _revise_dds(pack: dict, key: str, comment: str, router, gazetteer: Gazetteer) -> List[str]:
    idx = _find(pack["cards"], key)
    card = pack["cards"][idx]
    ctx = pack.get("generated")
    directives = parse_comment(comment)
    rng = random.Random("%s:%s:%s" % (pack["id"], key, comment))
    changes: List[str] = []
    kind = (card.get("gen") or {}).get("kind")
    builder = DdsBuilder(gazetteer, ctx, rng) if ctx and kind else None  # пакет из файла: правим только текст
    profiles = [c for c in pack["cards"][:idx] if (c.get("gen") or {}).get("kind") == "profile"]

    def rebuild(new_kind: str) -> Optional[dict]:
        if builder is None or not builder.can(new_kind) or (new_kind == "duplicate" and not profiles):
            return None
        for _ in range(20):
            new = builder.card(new_kind, key, profiles)
            if _check_route(router, pack, new):
                return dict(new, at=card["at"], source="генератор: " + KIND_TITLE[new_kind])
        return None

    if builder is not None and "harder" in directives:
        if kind == "profile":
            refs = _referenced(pack, key)
            options = ["other_uk", "oek", "lift_org", "road", "refusal", "duplicate"]
            rng.shuffle(options)
            new = None if refs else next((n for n in (rebuild(k) for k in options) if n), None)
            if new:
                card = new
                changes.append("карточка стала сложнее: %s, эталон — «%s»"
                               % (KIND_TITLE[new["gen"]["kind"]], new["expect"]["primary"]))
            elif refs:
                changes.append("на эту карточку ссылается дубль %s — сначала исправьте или исключите его" % ", ".join(refs))
        else:
            changes.append("карточка уже с подвохом (%s)" % KIND_TITLE.get(kind, kind))
    elif builder is not None and "easier" in directives:
        if kind != "profile":
            new = rebuild("profile")
            if new:
                card = new
                changes.append("карточка стала профильной: эталон — «Принята» и ход работ")
        else:
            changes.append("карточка уже профильная")
    kind = (card.get("gen") or {}).get("kind")
    if "type" in directives:
        if builder is not None and kind in ("profile", "other_uk") and not _referenced(pack, key):
            families = [f for f in builder.families if kind == "profile" or f in ("water", "electric", "roof")]
            fam = rng.choice([f for f in families if f != card["gen"]["family"]] or families)
            code, texts = rng.choice(sorted(FAMILIES[fam]["codes"].items()))
            card = dict(card, code=code, flags=list(FAMILIES[fam].get("flags", [])) if kind == "profile" else [],
                        description=rng.choice(texts).format(**_slots(rng)), gen=dict(card["gen"], family=fam))
            if kind == "profile":
                card["brigade"] = _brigade(fam, ctx["org"])
            changes.append("другой тип происшествия: код %s" % code)
        else:
            changes.append("тип не меняю: он задаёт правильный ответ этой карточки или на неё ссылается дубль")
    if "address" in directives:
        if builder is not None and kind in ("profile", "refusal", "oek") and not _referenced(pack, key):
            now = (card["address"]["street"], card["address"]["house"])
            street, house = rng.choice([a for a in ctx["own"] if tuple(a) != now] or ctx["own"])
            card = dict(card, address=dict(card["address"], street=street, house=house))
            changes.append("другой адрес из жилого фонда: %s" % _addr_text((street, house)))
        else:
            changes.append("адрес не меняю: по нему определяется правильный ответ (справка службы)")
    if "shorter" in directives:
        card["description"] = card["description"].split(",")[0].rstrip(".")
        changes.append("описание сокращено")
    if "longer" in directives:
        card["description"] = card["description"].rstrip(".") + ", заявитель просит прислать специалиста срочно"
        changes.append("описание дополнено")
    pack["cards"][idx] = card
    return changes


def _revise_call(pack: dict, key: str, comment: str, gazetteer: Gazetteer) -> List[str]:
    idx = _find(pack["calls"], key)
    call = pack["calls"][idx]
    gen = call.get("gen")
    if gen is None:
        return []  # вызов из билетов: эталон размечен вручную — править может только языковая модель
    directives = parse_comment(comment)
    rng = random.Random("%s:%s:%s" % (pack["id"], key, comment))
    changes: List[str] = []
    tpl = next(t for t in CALLS[gen["group"]] if t["code"] == gen["code"])
    group, difficulty, slots = gen["group"], gen["difficulty"], gen["slots"]
    person = (call["caller"]["name"], call["caller"]["gender"])
    where = (gen["district"], call["gold"]["address"]["street"], call["gold"]["address"]["house"])
    if "type" in directives:
        others = [t for t in CALLS[group] if t["code"] != tpl["code"]] or \
            [t for g in call_groups() if g != group for t in CALLS[g]]
        tpl = rng.choice(others)
        group = next(g for g in call_groups() if tpl in CALLS[g])
        changes.append("другой тип происшествия: код %s" % tpl["code"])
    if "address" in directives:
        district = rng.choice(sorted(gazetteer.districts))
        where = (district, rng.choice(gazetteer.districts[district]["streets"]), str(rng.randint(1, 45)))
        slots = _slots(rng)
        changes.append("новый адрес: %s, дом %s" % (where[1], where[2]))
    if "harder" in directives:
        if difficulty < 3:
            difficulty += 1
            changes.append("сложнее: " + ("заявитель сначала не называет точный адрес" if difficulty == 2
                                          else "заявитель взволнован, суть происшествия нужно выспрашивать"))
        else:
            changes.append("сложность уже высокая")
    elif "easier" in directives:
        if difficulty > 1:
            difficulty -= 1
            changes.append("проще: " + ("заявитель сразу рассказывает, что случилось" if difficulty == 2
                                        else "заявитель сразу называет точный адрес"))
        else:
            changes.append("сложность уже базовая")
    victims = tpl["victims"] if "type" in directives else call["gold"]["victims"]
    for want, value in (("victims", True), ("no_victims", False)):
        if want in directives:
            if tpl["victims_fixed"]:
                changes.append("пострадавшие заданы типом происшествия — не меняю, выберите «другой тип»")
            else:
                victims = value
                changes.append("есть пострадавшие" if value else "без пострадавших")
    if not changes:
        return changes
    new = _make_call(rng, tpl, group, gazetteer, key, difficulty, person=person, where=where, slots=slots)
    phone = call["caller"]["phone"]
    new["caller"]["phone"] = phone
    new["dialogue"]["phone"] = "Мой номер — %s." % phone
    new["situation"] = "%s, %s, %s" % (tpl["say"].format(**slots), person[0], phone)
    new["gold"]["victims"] = victims
    new["dialogue"]["victims"] = VICTIMS_SPEECH[victims]
    new["history"] = call.get("history", [])
    pack["calls"][idx] = new
    return changes


def exclude(pack: dict, key: str) -> dict:
    """Убрать карточку из черновика. Дубль, ссылающийся на неё, мешает — о нём скажем."""
    pack = copy.deepcopy(pack)
    field_ = "calls" if pack.get("mode") == "cards" else "cards"
    items = pack[field_]
    _find(items, key)
    refs = _referenced(pack, key)
    if refs:
        raise GeneratorError("на карточку %s ссылается дубль %s — сначала исключите его" % (key, ", ".join(refs)))
    if len(items) <= 1:
        raise GeneratorError("в сценарии должна остаться хотя бы одна карточка")
    pack[field_] = [c for c in items if c["key"] != key]
    if field_ == "cards":
        _retime(pack)
    return pack


def keep_only(pack: dict, keys: Sequence[str]) -> dict:
    """Частичное утверждение: остаются только отмеченные карточки (дубли — только вместе с исходной)."""
    field_ = "calls" if pack.get("mode") == "cards" else "cards"
    have = [c["key"] for c in pack[field_]]
    unknown = [k for k in keys if k not in have]
    if unknown:
        raise GeneratorError("нет карточек %s" % ", ".join(unknown))
    if not keys:
        raise GeneratorError("отметьте хотя бы одну карточку")
    out = pack
    for k in reversed([k for k in have if k not in set(keys)]):  # дубли стоят позже исходных — снимаем с конца
        out = exclude(out, k)
    return out


# ---------- карточки обучающихся (из режима «карточки») → поток ДДС ----------
def dds_card_from_student(row: dict, key: str, org: str) -> Optional[dict]:
    """Карточка 112, сохранённая обучающимся, как входящая карточка ДДС. Эталон — «Принята» и ход работ:
    справки об адресе для такой карточки нет, дом считается своим (допущение 4.6)."""
    saved = row.get("card") or {}
    card = saved.get("card") or {}
    code = row.get("code") or saved.get("code") or card.get("code")
    description = (card.get("description") or "").strip()
    addr = card.get("address") or {}
    if not code or not description or not (addr.get("street") or addr.get("descriptive")):
        return None
    flags = [f for f in card.get("flags") or [] if f != "ПП"]
    if card.get("victims") is True:
        flags.append("ПП")
    caller = card.get("caller") or {}
    fam = family_of(code) or "generic"
    address = {k: str(addr[k]) for k in ("street", "house", "entrance") if addr.get(k)}
    if not address.get("street"):
        address["street"] = str(addr.get("descriptive"))
    return {"key": key, "code": code, "flags": flags,
            "caller": {"name": (caller.get("name") or "Заявитель").split()[0], "status": caller.get("status") or "очевидец",
                       "phone": caller.get("phone") or "+79990000000"},
            "address": address, "description": description[:500],
            "expect": {"primary": "Принята", "final": "Работы завершены",
                       "why": "Карточку создал обучающийся в режиме «карточки»: происшествие профильное — «Принята», "
                              "затем статусы по докладам бригады."},
            "brigade": _brigade(fam, org),
            "gen": {"kind": "student", "family": fam,
                    "from": {"lesson": row.get("lesson_id"), "user": row.get("user_id"), "number": saved.get("number")}},
            "source": "карточка обучающегося, занятие L%s" % row.get("lesson_id")}


def usable_student_rows(rows: Sequence[dict], raw: dict, router, classifier) -> List[dict]:
    """Карточки обучающихся, годные для потока этой ДДС: тип выбран верно и карточка доходит до службы."""
    out = []
    for r in rows:
        saved = r.get("card") or {}
        if not r.get("code") or r["code"] not in classifier.incidents or {1, 12} & set(saved.get("categories") or []):
            continue
        card = dds_card_from_student(r, "X", "ГБУ")
        if card and not set(card["flags"]) - set(classifier.flags) and _check_route(router, raw, card):
            out.append(r)
    return out


def with_student_cards(base: dict, rows: Sequence[dict], source: str, count: int, router, classifier,
                       seed: str) -> dict:
    """Пакет занятия ДДС: карточки обучающихся или вперемешку с карточками сценария (ТЗ, сценарий 2)."""
    if source not in ("student", "mixed"):
        raise GeneratorError("источник карточек — pack, student или mixed")
    usable = usable_student_rows(rows, base, router, classifier)
    if not usable:
        raise GeneratorError("нет подходящих карточек обучающихся: проведите занятие в режиме «карточки» "
                             "с происшествиями, которые доходят до службы «%s»" % base.get("role_title", base["role"]))
    rng = random.Random(seed)
    picked = rng.sample(usable, min(count, len(usable)))
    org = (base.get("generated") or {}).get("org") or "ГБУ «Жилищник района %s»" % base["place"]["district"]
    students = [dds_card_from_student(r, "S%d" % (i + 1), org) for i, r in enumerate(picked)]
    raw = copy.deepcopy(base)
    raw["id"] = "%s+students" % base["id"]
    if source == "student":
        raw["title"] = "%s: карточки обучающихся" % base.get("role_title", base["role"])
        raw["cards"] = students
    else:
        raw["title"] = base["title"] + " + карточки обучающихся"
        own = sorted(raw["cards"], key=lambda c: c["at"])
        merged: List[dict] = []
        for i in range(max(len(own), len(students))):
            merged += own[i:i + 1] + students[i:i + 1]
        raw["cards"] = merged
    for i, card in enumerate(raw["cards"]):
        card["at"] = 10 + 60 * i
    return raw


# ---------- превью для преподавателя: эталон и вопросы с правильными ответами ----------
def preview(raw: dict, classifier, questionnaire_fn=None, checker=None) -> List[dict]:
    """Карточки сценария в виде, удобном для проверки: что увидит обучающийся и что считается правильным."""
    out = []
    if raw.get("mode") == "cards":
        for c in raw["calls"]:
            g = c["gold"]
            inc = classifier.incidents[g["codes"][0]]
            questions = []
            if questionnaire_fn is not None:
                q = questionnaire_fn(inc["group"])
                questions = [{"text": x["text"], "answer": x["flag"] in g["flags"]} for x in q["questions"]]
            out.append({"key": c["key"], "mode": "cards", "code": g["codes"][0], "type": inc["type"], "group": inc["group"],
                        "path": [x for x in (inc.get("attr1"), inc.get("attr2"), inc.get("attr3")) if x],
                        "situation": c["situation"], "dialogue": c["dialogue"], "flags": g["flags"],
                        "victims": g["victims"], "statuses": g["statuses"], "name": g["name"], "address": g["address"],
                        "facts": g["facts"], "questions": questions,
                        "difficulty": (c.get("gen") or {}).get("difficulty"), "history": c.get("history", []),
                        "issues": _issues(checker, [c["dialogue"]["opening"], c["dialogue"]["details"]])})
        return out
    for c in sorted(raw["cards"], key=lambda x: x["at"]):
        inc = classifier.incidents[c["code"]]
        kind = (c.get("gen") or {}).get("kind")
        texts = [c["description"]] + [c["expect"].get(k, "") for k in ("sample_comment", "sample_final_comment")]
        out.append({"key": c["key"], "mode": "card_actions", "at": c["at"], "code": c["code"], "type": inc["type"],
                    "group": inc["group"], "flags": c.get("flags", []), "address": c["address"],
                    "description": c["description"], "caller": c.get("caller", {}), "kind": kind,
                    "kind_title": KIND_TITLE.get(kind, "из файла сценария"), "expect": c["expect"],
                    "brigade": c.get("brigade", []), "history": c.get("history", []), "source": c.get("source", ""),
                    "issues": _issues(checker, texts)})
    return out


def _issues(checker, texts: Sequence[str]) -> List[dict]:
    if checker is None:
        return []
    found = []
    for t in texts:
        if t:
            found += [{"word": e["word"], "suggestions": e["suggestions"]}
                      for e in checker.spelling(re.sub(r"\{\w+\}", "", t))]
    return found
