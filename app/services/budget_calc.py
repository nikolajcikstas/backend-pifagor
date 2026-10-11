"""Расчёт бюджета компании за месяц: зарплаты с налогами, налоги компании,
прибыль, ЧП, ЧП на час, точка безубыточности, чувствительность.

Только арифметика — без базы данных и без шифрования, чтобы каждую формулу
можно было проверить отдельно (см. tests/test_budget_calc.py: цифры
бухгалтера за сентябрь 2026 сходятся до копейки).

Правила (Республика Беларусь, 2026):
• Подоходный налог 13% удерживается из начисленной зарплаты; база — начислено
  минус стандартный вычет (216 р в месяц, если доход за месяц не больше
  1 308 р и вычет применяется у этого работодателя).
• Взносы в ФСЗН: 34% платит организация сверху, 1% удерживается из зарплаты
  работника. База — всё начисленное, вычет её не уменьшает (за сентябрь:
  35% × 7 075,65 = 2 476,48 — ровно сумма, которую заплатили).
• Неоформленному человеку выплата идёт из выведенных средств: чтобы на руки
  пришло N, выводится N / 0,87, налог — N × 0,13 / 0,87.

ЧП = выручка − все расходы и налоги (включая налог 13/87 на вывод денег
«на следующий месяц»). Фонд и сумма «на следующий месяц» — распределение ЧП:
что остаётся после них — «свободный остаток».

Точка безубыточности — классический анализ «затраты — объём — прибыль»
(CVP): часы = постоянные расходы ÷ маржинальный доход на час
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
                "note": "не оформлен: выводится на руки, налог 13/87"}

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


def compute(data: dict, with_extras: bool = True) -> dict:
    """data: revenue, hours, lessons, tutors[{id,name,net,hours,lessons,formal,deduction}],
    staff[{id,name,net,formal,deduction}], consts, month{advertising,fund,next_month}."""
    c = consts_of(data.get("consts"))
    m = data.get("month") or {}
    R = float(data.get("revenue") or 0)
    H = float(data.get("hours") or 0)
    L = int(data.get("lessons") or 0)
    adv = float(m.get("advertising") or 0)
    fund = float(m.get("fund") or 0)
    nxt = float(m.get("next_month") or 0)
    it = c["income_tax_pct"] / 100

    tut = []
    for t in data.get("tutors") or []:
        line = payroll(t.get("net") or 0, t.get("formal", True), t.get("deduction", False), c)
        tut.append({**{k: t.get(k) for k in ("id", "name", "hours", "lessons")}, **line})
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
    # ЧП = выручка − все расходы (вместе с налогом 13/87 на вывод «следующего месяца»).
    # Фонд и сам «следующий месяц» — это распределение уже полученного ЧП.
    chp = r2(R - usn - easypay - acc - adv - tutors_cost - staff_cost - next_tax)
    rest = r2(chp - fund - nxt)

    out: dict[str, Any] = {
        "consts": c,
        "revenue": r2(R), "hours": round(H, 2), "lessons": L,
        "usn": usn, "easypay": easypay, "accountant": acc, "advertising": r2(adv),
        "tutors": tut, "staff": stf,
        "tutors_cost": tutors_cost, "staff_cost": staff_cost,
        "profit": chp, "fund": r2(fund), "next_month": r2(nxt), "next_month_tax": next_tax,
        "chp": chp, "rest": rest,
        "chp_per_hour": r2(chp / H) if H > 0 else None,
        "revenue_per_hour": r2(R / H) if H > 0 else None,
        "totals": {
            "net_to_people": r2(tot(tut, "net") + tot(stf, "net")),
            "gross": r2(tot(tut, "gross") + tot(stf, "gross")),
            "income_tax": r2(tot(tut, "income_tax") + tot(stf, "income_tax")),
            "pension": r2(tot(tut, "pension") + tot(stf, "pension")),
            "fszn": r2(tot(tut, "fszn") + tot(stf, "fszn")),
            "fszn_with_pension": r2(tot(tut, "fszn") + tot(stf, "fszn") + tot(tut, "pension") + tot(stf, "pension")),
            "belgosstrakh": r2(tot(tut, "belgosstrakh") + tot(stf, "belgosstrakh")),
            "withdraw_tax": r2(tot(tut, "withdraw_tax") + tot(stf, "withdraw_tax") + next_tax),
        },
    }
    out["waterfall"] = [
        {"key": "revenue", "label": "Выручка", "value": r2(R), "kind": "total"},
        {"key": "tutors", "label": "Репетиторы (с налогами)", "value": -tutors_cost, "kind": "minus"},
        {"key": "usn", "label": f"УСН {c['usn_pct']:g}%", "value": -usn, "kind": "minus"},
        {"key": "easypay", "label": f"EasyPay {c['easypay_pct']:g}%", "value": -easypay, "kind": "minus"},
        {"key": "staff", "label": "Сотрудники (с налогами)", "value": -staff_cost, "kind": "minus"},
        {"key": "accountant", "label": "Бухгалтер", "value": -acc, "kind": "minus"},
        {"key": "advertising", "label": "Реклама", "value": -r2(adv), "kind": "minus"},
        {"key": "next_tax", "label": "Налог 13/87 на вывод", "value": -next_tax, "kind": "minus"},
        {"key": "chp", "label": "ЧП", "value": chp, "kind": "total"},
        {"key": "fund", "label": "В фонд", "value": -r2(fund), "kind": "minus"},
        {"key": "next", "label": "На следующий месяц", "value": -r2(nxt), "kind": "minus"},
        {"key": "rest", "label": "Свободный остаток", "value": rest, "kind": "total"},
    ]

    # Безубыточность (CVP): выручка и зарплата репетиторов растут вместе с часами,
    # остальное — постоянные расходы месяца.
    be: dict[str, Any] = {"available": False}
    if H > 0 and R > 0:
        var_share = (c["usn_pct"] + c["easypay_pct"]) / 100
        rev_h = R / H
        tutor_h = tutors_cost / H
        margin_h = rev_h * (1 - var_share) - tutor_h
        fixed = acc + adv + staff_cost + next_tax
        alloc = fund + nxt
        be = {
            "available": True,
            "revenue_per_hour": r2(rev_h), "variable_per_hour": r2(rev_h * var_share + tutor_h),
            "tutor_cost_per_hour": r2(tutor_h), "margin_per_hour": r2(margin_h),
            "fixed": r2(fixed), "allocations": r2(alloc),
        }
        if margin_h > 0:
            h0 = fixed / margin_h
            h1 = (fixed + alloc) / margin_h
            be.update({
                "hours_breakeven": round(h0, 1),
                "hours_target": round(h1, 1),
                "safety_pct": round(100 * (H - h0) / H, 1),
                "operating_leverage": round(margin_h * H / chp, 2) if abs(chp) > 0.01 else None,
            })
            top = max(H * 1.6, h1 * 1.25, 10)
            pts = []
            steps = 24
            for i in range(steps + 1):
                h = top * i / steps
                pts.append({"h": round(h, 1), "chp": r2(margin_h * h - fixed), "rest": r2(margin_h * h - fixed - alloc)})
            be["curve"] = pts
        else:
            be["note"] = "Каждый час приносит меньше, чем стоит: при этих ценах и ставках безубыточности нет."
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
    ("volume", "Количество часов (учеников)"),
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
