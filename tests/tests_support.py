"""Общие помощники тестов."""


def perfect_card(call: dict, number: str = "1") -> dict:
    """Карточка 112, заполненная ровно по эталону вызова."""
    g = call["gold"]
    address = {k: v for k, v in g["address"].items() if k != "kw"}
    if g["address"].get("kw"):
        address["descriptive"] = " ".join(g["address"]["kw"])
    if address.get("subject") is None:
        address["subject"] = "Москва"
    return {"number": number, "code": g["codes"][0], "flags": [f for f in g["flags"] if f != "ПП"],
            "victims": True if g["victims"] is None else g["victims"],
            "caller": {"name": g["name"] or call["caller"]["name"], "status": (g["statuses"] or ["очевидец"])[0]},
            "address": address, "description": ". ".join(f[0] for f in g["facts"]) + "."}


def sample_comment(expect: dict, key: str, numbers: dict) -> str:
    """Образец комментария из эталона; {A} — номер карточки A в занятии."""
    text = expect[key]
    for k, num in numbers.items():
        text = text.replace("{%s}" % k, num)
    return text


def perfect_run(engine, clock, pack, comment=sample_comment):
    """Обучающийся действует ровно по эталону — должен получить 100.
    comment(expect, "sample_comment" | "sample_final_comment", номера карточек) — текст комментария."""
    from domain.lesson import Actor, ActorKind
    teacher = Actor("t1", ActorKind.TEACHER)
    rt = engine.create_lesson(teacher, pack.id)
    L = rt.lesson.id
    engine.join(L, "s1", "S1")
    engine.assign_role(teacher, L, "s1", pack.role)
    engine.start(teacher, L)
    t0, seq = clock.now, 0

    def send(number, status, text):
        nonlocal seq
        seq += 1
        out = engine.handle(L, "s1", {"t": "set_status", "card": number, "status": status, "comment": text, "seq": seq})
        assert not [m for _, m in out if m["t"] == "status_rejected"], out

    for t in range(0, max(c["at"] for c in pack.cards) + 400, 5):
        clock.now = t0 + t
        for to, m in engine.tick():
            if to != "s1":
                continue
            if m["t"] == "card":
                number = m["card"]["number"]
                exp = rt.cards[number].spec["expect"]
                if exp["primary"] == "Не принята":
                    send(number, "Не принята", comment(exp, "sample_comment", rt.numbers))
                else:
                    send(number, "Принята", "Принята, направлена бригада.")
            elif m["t"] == "call":
                spec = rt.cards[m["card"]].spec
                rep = next(r for r in spec["brigade"] if r["text"] == m["text"])
                text = comment(spec["expect"], "sample_final_comment", rt.numbers)                     if rep["status"] == "Отказ от выполнения работ" else rep["text"]
                send(m["card"], rep["status"], text)
    engine.finish(teacher, L)
    return rt.results["participants"][0]
