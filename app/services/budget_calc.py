"""Расчёт бюджета компании за месяц: зарплаты с налогами, налоги компании,
прибыль, ЧП, ЧП на час, точка безубыточности, чувствительность.

Только арифметика — без базы данных и без шифрования, чтобы каждую формулу
можно было проверить отдельно (см. tests/test_budget_calc.py: цифры
бухгалтера за сентябрь 2026 сходятся до копейки).

Правила (Республика Беларусь, 2026):
• Человек получает на карту ровно заработанное; все налоги и взносы — расход
  организации. Бухгалтер начисляет сумму G так, чтобы после подоходного 13%
  (с G минус стандартный вычет 216 р, если доход за месяц не больше 1 308 р)
  и 1% в ФСЗН на карту пришло заработанное.
• Взносы в ФСЗН: 34% + 1% от всего начисленного (вычет базу не уменьшает),
  Белгосстрах — 0,6% от начисленного.
• Неоформленному человеку выплата идёт из выведенных средств: чтобы на руки
  пришло N, выводится N / 0,87, налог — N × 0,13 / 0,87.

• Реклама оплачивается с личной карты: деньги выводятся со счёта, подоходный
  налог на вывод — реклама × 0,13 / 0,87.

ЧП = выручка − все расходы и налоги (включая подоходный налог на вывод денег
на рекламу и «на следующий месяц»). Фонд и сумма «на следующий месяц» — распределение ЧП:
что остаётся после них — «свободный остаток».

Точка безубыточности — классический анализ «затраты — объём — прибыль»
(CVP) в занятиях: занятия = постоянные расходы ÷ маржинальный доход занятия
(цена клиента и ставка репетитора — за занятие, часы не учитываются)
(Horngren, Datar, Rajan. Cost Accounting: A Managerial Emphasis, гл. 3;
Drury. Management and Cost Accounting, гл. 8).
Чувствительность — изменение по одному параметру на ±10% при прочих равных
(one-at-a-time; Saltelli et al. Global Sensitivity Analysis. The Primer, 2008).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import Any, Optional

DEFAULT_CONSTS = {
    "usn_pct": 6.0,            # УСН, % от выручки
    "easypay_pct": 1.2,        # комиссия EasyPay, % от выручки
    "accountant": 450.0,       # бухгалтерские услуги, р в месяц
    "income_tax_pct": 13.0,    # подоходный налог, %
    "fszn_pct": 34.0,          # взносы ФСЗН за счёт организации, %
    "pension_pct": 1.0,        # пенсионный взнос работника (удерживается), %
    "deduction": 216.0,        # стандартный вычет, р в месяц
    "deduction_limit": 1308.0, # вычет — только если начислено за месяц не больше
    "belgosstrakh_pct": 0.6,   # страхование от несчастных случаев (Белгосстрах), % от начисленного
}


def r2(x: float) -> float:
    """Округление до копеек «как в бухгалтерии» (0,005 → 0,01)."""
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _pct(x: float) -> str:
    return f"{x:g}".replace(".", ",") + "%"


def consts_of(c: Optional[dict]) -> dict:
    out = dict(DEFAULT_CONSTS)
    for k, v in (c or {}).items():
        if k in out and v is not None:
            out[k] = float(v)
    return out


def payroll(net: float, formal: bool, deduction: bool, c: dict) -> dict:
    """Сколько стоит выплатить человеку `net` рублей на руки.

    Оформлен (трудовой договор / подряд): ищем начисленное G, при котором
    G − подоходный − 1% = net.
      • если G не больше вычета: подоходного нет, net = G × 0,99 → G = net / 0,99;
      • иначе: net = G − 0,13 × (G − вычет) − 0,01 × G → G = (net − 0,13 × вычет) / 0,86.
    Если с вычетом G получается больше порога (1 308 р) — вычет не положен,
    считаем без него. Организация платит ещё 34% от G в ФСЗН (и взнос в
    Белгосстрах, если его ставка указана).
    """
    net = max(0.0, float(net or 0))
    it, p, f = c["income_tax_pct"] / 100, c["pension_pct"] / 100, c["fszn_pct"] / 100
    bg = c.get("belgosstrakh_pct", 0.0) / 100
    base = {"net": r2(net), "formal": bool(formal), "deduction_asked": bool(deduction)}
    if net <= 0:
        return {**base, "gross": 0.0, "income_tax": 0.0, "pension": 0.0, "fszn": 0.0, "belgosstrakh": 0.0,
                "withdraw_tax": 0.0, "cost": 0.0, "deduction_applied": 0.0, "note": None}
    if not formal:
        tax = r2(net * it / (1 - it))
        return {**base, "gross": r2(net + tax), "income_tax": 0.0, "pension": 0.0, "fszn": 0.0, "belgosstrakh": 0.0,
                "withdraw_tax": tax, "cost": r2(net + tax), "deduction_applied": 0.0,
                "note": "не оформлен: выплата из выведенных средств, подоходный налог на вывод"}

    def gross_for(d: float) -> float:
        g = net / (1 - p)
        if g <= d:
            return g
        return (net - it * d) / (1 - it - p)

    d = c["deduction"] if deduction else 0.0
    note = None
    g = gross_for(d)
    if d > 0 and c["deduction_limit"] and g > c["deduction_limit"]:
        d, g = 0.0, gross_for(0.0)
        note = f"вычет не положен: начислено больше {c['deduction_limit']:g} р"
    # Начисленное округляем до копейки вверх и проверяем, что на карту
    # приходит не меньше нужного (бухгалтер подбирает сумму так же).
    gross = float((Decimal(str(g)) - Decimal("0.000001")).quantize(Decimal("0.01"), rounding=ROUND_CEILING))
    for _ in range(5):
        if r2(gross - r2(it * max(0.0, gross - d)) - r2(p * gross)) >= r2(net):
            break
        gross = r2(gross + 0.01)
    income_tax = r2(it * max(0.0, gross - d))
    pension = r2(p * gross)
    fszn = r2(f * gross)
    bgs = r2(bg * gross)
    return {**base, "gross": gross, "income_tax": income_tax, "pension": pension, "fszn": fszn, "belgosstrakh": bgs,
            "withdraw_tax": 0.0, "cost": r2(gross + fszn + bgs), "deduction_applied": r2(min(d, gross)),
            "net_check": r2(gross - income_tax - pension), "note": note}


def _item_view(i: dict) -> dict:
    return {"id": i.get("id"), "name": i.get("name") or "Статья", "mode": i.get("mode", "fixed"),
            "value": float(i.get("value") or 0), "kind": "alloc" if i.get("mode") == "pct" else i.get("kind", "alloc"),
            "withdraw": bool(i.get("withdraw"))}


def compute(data: dict, with_extras: bool = True) -> dict:
    """data: revenue, hours, lessons, tutors[{id,name,net,hours,lessons,formal,deduction}],
    staff[{id,name,net,formal,deduction}], consts, month{advertising,fund,next_month}."""
    c = consts_of(data.get("consts"))
    m = data.get("month") or {}
    R = float(data.get("revenue") or 0)
    H = float(data.get("hours") or 0)
    L = float(data.get("lessons") or 0)
    adv = float(m.get("advertising") or 0)
    fund = float(m.get("fund") or 0)
    nxt = float(m.get("next_month") or 0)
    it = c["income_tax_pct"] / 100

    tut = []
    for t in data.get("tutors") or []:
        line = payroll(t.get("net") or 0, t.get("formal", True), t.get("deduction", False), c)
        row = {**{k: t.get(k) for k in ("id", "name", "hours", "lessons")}, **line}
        if t.get("revenue") is not None:
            # что репетитор приносит: выручка его занятий − УСН и EasyPay с неё − его стоимость с налогами
            rv = float(t["revenue"])
            row["revenue"] = r2(rv)
            row["contribution"] = r2(rv - rv * (c["usn_pct"] + c["easypay_pct"]) / 100 - line["cost"])
        tut.append(row)
    stf = []
    for s in data.get("staff") or []:
        line = payroll(s.get("net") or 0, s.get("formal", True), s.get("deduction", False), c)
        stf.append({**{k: s.get(k) for k in ("id", "name")}, **line})

    def tot(rows, k):
        return r2(sum(x[k] for x in rows))

    tutors_cost, staff_cost = tot(tut, "cost"), tot(stf, "cost")
    usn = r2(R * c["usn_pct"] / 100)
    easypay = r2(R * c["easypay_pct"] / 100)
    acc = r2(c["accountant"])
    next_tax = r2(nxt * it / (1 - it))
    # Реклама оплачивается с личной карты — деньги на неё выводятся со счёта.
    adv_tax = r2(adv * it / (1 - it))
    # Фонд и сам «следующий месяц» — это распределение уже полученного ЧП.
    kw = it / (1 - it)

    # Свои статьи затрат. «Расход» уменьшает ЧП; «из ЧП» (фиксированная сумма
    # или % от ЧП) — распределение, как фонд. Если статья выводится на карту —
    # подоходный налог на вывод (×13/87) — это расход и входит в ЧП.
    items_in = [i for i in (m.get("items") or []) if float(i.get("value") or 0) > 0]
    items_out: list[dict] = []
    exp_amt = exp_tax = alloc_fixed = alloc_fixed_tax = 0.0
    for i in items_in:
        if i.get("mode") == "pct":
            continue
        v = r2(float(i["value"]))
        tax = r2(v * kw) if i.get("withdraw") else 0.0
        if i.get("kind") == "expense":
            exp_amt += v
            exp_tax += tax
        else:
            alloc_fixed += v
            alloc_fixed_tax += tax
        items_out.append({**_item_view(i), "amount": v, "tax": tax})
    pct_items = [i for i in items_in if i.get("mode") == "pct"]
    pw = sum(float(i["value"]) / 100 for i in pct_items if i.get("withdraw"))
    p_all = sum(float(i["value"]) / 100 for i in pct_items)

    # ЧП = выручка − все расходы (вместе с подоходным налогом на вывод денег
    # на рекламу, «на следующий месяц» и свои статьи, выводимые на карту).
    base = r2(R - usn - easypay - acc - adv - adv_tax - tutors_cost - staff_cost - next_tax
              - exp_amt - exp_tax - alloc_fixed_tax)
    # Статьи «% от ЧП» с выводом на карту: их налог сам уменьшает ЧП, поэтому
    # ЧП = база ÷ (1 + 13/87 × доля выводимых процентов).
    chp_raw = base / (1 + kw * pw) if base > 0 else base
    pct_tax_total = 0.0
    pct_amt_total = 0.0
    for i in pct_items:
        a = r2(float(i["value"]) / 100 * max(chp_raw, 0.0))
        tax = r2(a * kw) if i.get("withdraw") else 0.0
        pct_amt_total += a
        pct_tax_total += tax
        items_out.append({**_item_view(i), "amount": a, "tax": tax})
    chp = r2(base - pct_tax_total)
    items_tax = r2(exp_tax + alloc_fixed_tax + pct_tax_total)
    items_alloc = r2(alloc_fixed + pct_amt_total)
    rest = r2(chp - fund - nxt - items_alloc)
    out: dict[str, Any] = {
        "consts": c,
        "revenue": r2(R), "hours": round(H, 2), "lessons": int(round(L)),
        "usn": usn, "easypay": easypay, "accountant": acc, "advertising": r2(adv), "advertising_tax": adv_tax,
        "tutors": tut, "staff": stf,
        "tutors_cost": tutors_cost, "staff_cost": staff_cost,
        "profit": chp, "fund": r2(fund), "next_month": r2(nxt), "next_month_tax": next_tax,
        "items": items_out, "items_expense": r2(exp_amt), "items_tax": items_tax, "items_alloc": items_alloc,
        "chp": chp, "rest": rest,
        # всё «на занятие»: ставка репетитора и цена клиента — за занятие, часы не учитываются
        "chp_per_lesson": r2(chp / L) if L > 0 else None,
        "rest_per_lesson": r2(rest / L) if L > 0 else None,
        "revenue_per_lesson": r2(R / L) if L > 0 else None,
        "totals": {
            "net_to_people": r2(tot(tut, "net") + tot(stf, "net")),
            "gross": r2(tot(tut, "gross") + tot(stf, "gross")),
            "income_tax": r2(tot(tut, "income_tax") + tot(stf, "income_tax")),
            "pension": r2(tot(tut, "pension") + tot(stf, "pension")),
            "fszn": r2(tot(tut, "fszn") + tot(stf, "fszn")),
            "fszn_with_pension": r2(tot(tut, "fszn") + tot(stf, "fszn") + tot(tut, "pension") + tot(stf, "pension")),
            "belgosstrakh": r2(tot(tut, "belgosstrakh") + tot(stf, "belgosstrakh")),
            "withdraw_tax": r2(tot(tut, "withdraw_tax") + tot(stf, "withdraw_tax") + adv_tax + next_tax + items_tax),
        },
    }
    T = out["totals"]
    T["taxes_all"] = r2(T["income_tax"] + T["pension"] + T["fszn"] + T["belgosstrakh"] + T["withdraw_tax"] + usn)
    T["taxes_share_pct"] = round(100 * T["taxes_all"] / R, 1) if R > 0 else None
    out["waterfall"] = [
        {"key": "revenue", "label": "Выручка", "value": r2(R), "kind": "total"},
        {"key": "tutors", "label": "Репетиторы\n(с налогами)", "value": -tutors_cost, "kind": "minus"},
        {"key": "usn", "label": f"УСН\n{_pct(c['usn_pct'])}", "value": -usn, "kind": "minus"},
        {"key": "easypay", "label": f"EasyPay\n{_pct(c['easypay_pct'])}", "value": -easypay, "kind": "minus"},
        {"key": "staff", "label": "Сотрудники\n(с налогами)", "value": -staff_cost, "kind": "minus"},
        {"key": "accountant", "label": "Бухгалтер", "value": -acc, "kind": "minus"},
        {"key": "advertising", "label": "Реклама", "value": -r2(adv), "kind": "minus"},
        *[{"key": f"item_{x['id']}", "label": x["name"], "value": -x["amount"], "kind": "minus"}
          for x in items_out if x["kind"] == "expense"],
        {"key": "next_tax", "label": "Подоходный налог\nна вывод", "value": -r2(adv_tax + next_tax + items_tax), "kind": "minus"},
        {"key": "chp", "label": "ЧП", "value": chp, "kind": "total"},
        {"key": "fund", "label": "В фонд", "value": -r2(fund), "kind": "minus"},
        {"key": "next", "label": "На следующий\nмесяц", "value": -r2(nxt), "kind": "minus"},
        *[{"key": f"item_{x['id']}", "label": x["name"], "value": -x["amount"], "kind": "minus"}
          for x in items_out if x["kind"] == "alloc"],
        {"key": "rest", "label": "Свободный\nостаток", "value": rest, "kind": "total"},
    ]

    # Безубыточность (CVP) в занятиях: выручка и зарплата репетиторов растут вместе
    # с числом занятий (цена клиента и ставка — за занятие), остальное — постоянные
    # расходы месяца.
    be: dict[str, Any] = {"available": False}
    if L > 0 and R > 0:
        var_share = (c["usn_pct"] + c["easypay_pct"]) / 100
        rev_l = R / L
        tutor_l = tutors_cost / L
        margin_l = rev_l * (1 - var_share) - tutor_l
        fixed = acc + adv + adv_tax + staff_cost + next_tax + exp_amt + exp_tax + alloc_fixed_tax
        alloc = fund + nxt + alloc_fixed

        def chp_at(num: float) -> float:   # ЧП, когда «маржа × занятия − постоянные» = num
            return num / (1 + kw * pw) if num > 0 else num
        be = {
            "available": True,
            "revenue_per_lesson": r2(rev_l), "variable_per_lesson": r2(rev_l * var_share + tutor_l),
            "tutor_cost_per_lesson": r2(tutor_l), "margin_per_lesson": r2(margin_l),
            "fixed": r2(fixed), "allocations": r2(alloc),
        }
        if margin_l > 0:
            n0 = fixed / margin_l
            # остаток = ЧП × (1 − доля %-статей) − фиксированные распределения = 0
            if p_all < 1:
                need = alloc / (1 - p_all)
                n1 = (fixed + need * (1 + kw * pw)) / margin_l
            else:
                n1 = None
            be.update({
                "lessons_breakeven": round(n0, 1),
                "lessons_target": round(n1, 1) if n1 is not None else None,
                "safety_pct": round(100 * (L - n0) / L, 1),
                "operating_leverage": round(margin_l * L / chp, 2) if abs(chp) > 0.01 else None,
            })
            top = max(L * 1.6, (n1 or n0) * 1.25, 10)
            steps = 24
            curve = []
            for i in range(steps + 1):
                n = top * i / steps
                c_ = chp_at(margin_l * n - fixed)
                curve.append({"n": round(n, 1), "chp": r2(c_), "rest": r2(c_ - alloc - p_all * max(c_, 0.0))})
            be["curve"] = curve
        else:
            be["note"] = "Каждое занятие приносит меньше, чем стоит: при этих ценах и ставках безубыточности нет."
    out["breakeven"] = be

    if with_extras:
        out["sensitivity"] = _sensitivity(data, chp)
    return out


def _scaled(data: dict, key: str, k: float) -> dict:
    d = {**data, "consts": dict(consts_of(data.get("consts"))), "month": dict(data.get("month") or {}),
         "tutors": [dict(t) for t in data.get("tutors") or []], "staff": [dict(s) for s in data.get("staff") or []]}
    if key == "price":
        d["revenue"] = float(d.get("revenue") or 0) * k
    elif key == "volume":
        d["revenue"] = float(d.get("revenue") or 0) * k
        d["hours"] = float(d.get("hours") or 0) * k
        d["lessons"] = float(d.get("lessons") or 0) * k
        for t in d["tutors"]:
            t["net"] = float(t.get("net") or 0) * k
    elif key == "tutor_rate":
        for t in d["tutors"]:
            t["net"] = float(t.get("net") or 0) * k
    elif key == "staff":
        for s in d["staff"]:
            s["net"] = float(s.get("net") or 0) * k
    elif key in ("advertising",):
        d["month"][key] = float(d["month"].get(key) or 0) * k
    elif key in ("usn_pct", "accountant"):
        d["consts"][key] = d["consts"][key] * k
    return d


SENS_PARAMS = [
    ("price", "Цена занятия"),
    ("volume", "Количество занятий"),
    ("tutor_rate", "Ставки репетиторов"),
    ("staff", "Зарплаты сотрудников"),
    ("advertising", "Реклама"),
    ("usn_pct", "Ставка УСН"),
    ("accountant", "Бухгалтер"),
]


def _sensitivity(data: dict, base: float) -> list[dict]:
    rows = []
    for key, label in SENS_PARAMS:
        lo = compute(_scaled(data, key, 0.9), with_extras=False)["chp"]
        hi = compute(_scaled(data, key, 1.1), with_extras=False)["chp"]
        rows.append({
            "key": key, "label": label, "minus10": lo, "plus10": hi,
            "delta_minus": r2(lo - base), "delta_plus": r2(hi - base),
            "elasticity": round((hi - lo) / (0.2 * base), 2) if abs(base) > 0.01 else None,
        })
    rows.sort(key=lambda x: -abs(x["plus10"] - x["minus10"]))
    return rows
