"""Сверка расчёта зарплат с таблицей бухгалтера за сентябрь 2026."""
from app.services.budget_calc import payroll, compute, consts_of, r2

# имя, на карту, начислено, ПН, 1%, вычет применён
SEPT = [
    ("Шпакова", 892.12, 1004.70, 102.53, 10.05, True),
    ("Тихонович", 132.11, 133.44, 0.00, 1.33, True),
    ("Федоренко К.", 676.56, 754.05, 69.95, 7.54, True),
    ("Рапчинский", 693.59, 773.85, 72.52, 7.74, True),
    ("Матюхин", 252.03, 260.40, 5.77, 2.60, True),
    ("Борщов", 627.03, 696.45, 62.46, 6.96, True),
    ("Федоренко А.", 840.05, 976.80, 126.98, 9.77, False),
    ("Мухарский", 384.02, 413.88, 25.72, 4.14, True),
    ("Бобр", 156.10, 157.68, 0.00, 1.58, True),
    ("Пунтус", 396.09, 427.92, 27.55, 4.28, True),
    ("Меньшикова", 468.02, 511.56, 38.42, 5.12, True),
    ("Евтеев", 255.12, 264.00, 6.24, 2.64, True),
    ("Булыгина", 228.08, 232.56, 2.15, 2.33, True),
    ("Ермолович", 264.00, 274.32, 7.58, 2.74, True),
    ("Храпко", 144.10, 145.56, 0.00, 1.46, True),
    ("Шаповал", 48.00, 48.48, 0.00, 0.48, True),
]


def test_september_lines_match_accountant():
    c = consts_of({"belgosstrakh_pct": 0})
    for name, net, gross, pn, pen, ded in SEPT:
        x = payroll(net, True, ded, c)
        assert abs(x["gross"] - gross) <= 0.02, (name, x["gross"], gross)
        assert abs(x["income_tax"] - pn) <= 0.02, (name, x["income_tax"], pn)
        assert abs(x["pension"] - pen) <= 0.01, (name, x["pension"], pen)


def test_september_fszn_total():
    # ФСЗН: 34% организации + 1% работника от всего начисленного = 2 476,48
    gross_total = sum(g for _, _, g, _, _, _ in SEPT)
    assert r2(gross_total * 0.35) == 2476.48
    c = consts_of({"belgosstrakh_pct": 0})
    lines = [payroll(net, True, ded, c) for _, net, _, _, _, ded in SEPT]
    fszn = sum(x["fszn"] + x["pension"] for x in lines)
    assert abs(fszn - 2476.48) <= 0.15, fszn


def test_deduction_limit():
    c = consts_of(None)
    x = payroll(1200, True, True, c)  # начислено > 1308 — вычета нет
    assert x["deduction_applied"] == 0 and x["gross"] == r2(1200 / 0.86)


def test_informal_and_chp():
    c = consts_of(None)
    x = payroll(870, False, False, c)
    assert x["withdraw_tax"] == 130.0 and x["cost"] == 1000.0
    res = compute({"revenue": 10000, "hours": 200, "lessons": 200,
                   "tutors": [{"id": 1, "name": "A", "net": 3000, "formal": True, "deduction": True}],
                   "staff": [], "month": {"advertising": 100, "fund": 500, "next_month": 870}})
    # ЧП = 10000 − УСН 600 − EasyPay 120 − бухгалтер 450 − реклама 100 − налог на вывод на рекламу
    #      − репетитор − налог на вывод «следующего месяца» 130
    t = res["tutors"][0]
    assert res["advertising_tax"] == r2(100 * 13 / 87) == 14.94
    assert res["chp"] == r2(10000 - 600 - 120 - 450 - 100 - 14.94 - t["cost"] - 130)
    # свободный остаток = ЧП − фонд − следующий месяц
    assert res["rest"] == r2(res["chp"] - 500 - 870)
    be = res["breakeven"]
    # при h = точке безубыточности прибыль ≈ 0
    m = be["margin_per_lesson"]
    assert abs(m * be["lessons_breakeven"] - be["fixed"]) < m * 0.06  # округление до 0,1 занятия
    # всё «на занятие»: часы в расчёт не входят
    res2 = compute({"revenue": 10000, "hours": 999, "lessons": 200,
                    "tutors": [{"id": 1, "name": "A", "net": 3000, "formal": True, "deduction": True}],
                    "staff": [], "month": {"advertising": 100, "fund": 500, "next_month": 870}})
    assert res2["breakeven"] == be and res2["chp"] == res["chp"]
    assert res["chp_per_lesson"] == r2(res["chp"] / 200) and res["rest_per_lesson"] == r2(res["rest"] / 200)


def test_net_never_short_and_minimal():
    """На карту приходит не меньше нужного и не больше чем на 1 копейку сверх."""
    import random
    c = consts_of(None)
    rnd = random.Random(7)
    for _ in range(3000):
        net = round(rnd.uniform(0.01, 3000), 2)
        for ded in (True, False):
            x = payroll(net, True, ded, c)
            assert x["net_check"] >= net, (net, ded, x)
            assert x["net_check"] - net <= 0.011, (net, ded, x)


def test_belgosstrakh_in_cost():
    c = consts_of(None)  # по умолчанию 0,6%
    x = payroll(892, True, True, c)
    assert x["belgosstrakh"] == r2(0.006 * x["gross"])
    assert x["cost"] == r2(x["gross"] + x["fszn"] + x["belgosstrakh"])


def test_structure_parts_sum_to_revenue_and_contribution():
    """Части «куда уходит выручка» в сумме дают выручку; вклад репетитора =
    выручка его занятий − УСН и EasyPay с неё − его стоимость с налогами."""
    res = compute({"revenue": 5000, "hours": 100, "lessons": 100,
                   "tutors": [{"id": 1, "name": "A", "net": 1500, "formal": True, "deduction": True, "revenue": 3000},
                              {"id": 2, "name": "B", "net": 900, "formal": False, "deduction": False, "revenue": 2000}],
                   "staff": [{"id": "s", "name": "M", "net": 600, "formal": True, "deduction": False}],
                   "month": {"advertising": 200, "fund": 0, "next_month": 300}})
    T = res["totals"]
    people = sum(x.get("net_check", x["net"]) if x["formal"] else x["net"] for x in res["tutors"] + res["staff"])
    parts = people + res["accountant"] + res["advertising"] + res["easypay"] + T["taxes_all"] + res["chp"]
    assert abs(parts - 5000) < 0.02, parts
    a = res["tutors"][0]
    assert a["contribution"] == r2(3000 - 3000 * 0.072 - a["cost"])
    # налог на вывод: неоформленный B + реклама + следующий месяц
    assert T["withdraw_tax"] == r2(res["tutors"][1]["withdraw_tax"] + r2(200 * 13 / 87) + r2(300 * 13 / 87))


def _base_data(items):
    return {"revenue": 10000, "hours": 0, "lessons": 250,
            "tutors": [{"id": 1, "name": "A", "net": 3000, "formal": True, "deduction": False}],
            "staff": [], "month": {"advertising": 0, "fund": 500, "next_month": 0, "items": items}}


def test_custom_items():
    k = 13 / 87
    base = compute(_base_data([]))
    # фиксированный расход, выводится на карту: ЧП меньше на сумму и налог на вывод
    r = compute(_base_data([{"id": "rent", "name": "Аренда", "mode": "fixed", "value": 300, "kind": "expense", "withdraw": True}]))
    assert r["chp"] == r2(base["chp"] - 300 - r2(300 * k))
    assert r["rest"] == r2(r["chp"] - 500)
    # фиксированная сумма «из ЧП», остаётся на счёте: ЧП не меняется, остаток меньше
    r = compute(_base_data([{"id": "f", "name": "Резерв", "mode": "fixed", "value": 200, "kind": "alloc", "withdraw": False}]))
    assert r["chp"] == base["chp"] and r["rest"] == r2(base["rest"] - 200)
    # 5% от ЧП на карту: налог сам уменьшает ЧП; сумма = 5% от итогового ЧП
    r = compute(_base_data([{"id": "bd", "name": "День рождения", "mode": "pct", "value": 5, "kind": "alloc", "withdraw": True}]))
    it = r["items"][0]
    assert abs(it["amount"] - 0.05 * r["chp"]) <= 0.02
    assert it["tax"] == r2(it["amount"] * k)
    assert r["chp"] == r2(base["chp"] - it["tax"])
    assert r["rest"] == r2(r["chp"] - 500 - it["amount"])
    # 5% от ЧП без вывода: ЧП тот же, остаток меньше на 5% ЧП
    r = compute(_base_data([{"id": "bd", "name": "ДР", "mode": "pct", "value": 5, "kind": "alloc", "withdraw": False}]))
    assert r["chp"] == base["chp"] and r["rest"] == r2(base["rest"] - r2(0.05 * base["chp"]))
    # сверка: выручка − выплаты − все налоги и комиссии − расходы = ЧП
    r = compute(_base_data([{"id": "rent", "name": "Аренда", "mode": "fixed", "value": 300, "kind": "expense", "withdraw": True},
                            {"id": "bd", "name": "ДР", "mode": "pct", "value": 5, "kind": "alloc", "withdraw": True}]))
    T = r["totals"]
    paid = sum(x.get("net_check", x["net"]) for x in r["tutors"])
    assert abs(10000 - paid - r["accountant"] - r["easypay"] - T["taxes_all"] - r["items_expense"] - r["chp"]) < 0.02
    # безубыточность: на точке безубыточности ЧП = 0
    be = r["breakeven"]
    pts = be["curve"]
    assert be["lessons_breakeven"] > 0 and any(p["chp"] > 0 for p in pts)
