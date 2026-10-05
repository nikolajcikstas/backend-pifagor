"""Цена занятия ученика с историей.

Пока у ученика нет истории цен, всё работает как раньше: одна цена
ChildProfile.lesson_price для всех занятий. Как только цену меняют «с даты»
(скидка за рекомендации или ручная правка при уже существующей истории),
каждое занятие считается по цене, действовавшей на его дату, а поле
lesson_price показывает цену, действующую сегодня.
"""
from bisect import bisect_right
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import ChildProfile, Lesson, StudentPriceHistory

BASE_PRICE = 40.0          # скидки за рекомендации — только при этой цене
DISCOUNT_STEPS = ((4, 10), (2, 5))  # заявок «больше 3» → 10%, «2–3» → 5%


def minsk_today() -> date:
    return (datetime.utcnow() + timedelta(hours=3)).date()


class PriceBook:
    """Цены учеников по датам: price(child_id, day)."""

    def __init__(self, current: dict[int, float], history: dict[int, tuple[list, list]]):
        self.current = current
        self.history = history

    def has_history(self, cid: int) -> bool:
        return cid in self.history

    def price(self, cid: int, d: date) -> float:
        h = self.history.get(cid)
        if h:
            i = bisect_right(h[0], d)
            return h[1][i - 1] if i else h[1][0]
        return self.current.get(cid) or BASE_PRICE


async def load_prices(db: AsyncSession, child_ids: Optional[Iterable[int]] = None) -> PriceBook:
    q = select(ChildProfile.id, ChildProfile.lesson_price)
    hq = select(StudentPriceHistory.child_id, StudentPriceHistory.effective_from, StudentPriceHistory.price) \
        .order_by(StudentPriceHistory.child_id, StudentPriceHistory.effective_from, StudentPriceHistory.id)
    if child_ids is not None:
        ids = list(set(child_ids))
        if not ids:
            return PriceBook({}, {})
        q = q.where(ChildProfile.id.in_(ids))
        hq = hq.where(StudentPriceHistory.child_id.in_(ids))
    current = {cid: (p or BASE_PRICE) for cid, p in (await db.execute(q)).all()}
    history: dict[int, tuple[list, list]] = {}
    for cid, eff, price in (await db.execute(hq)).all():
        days, prices = history.setdefault(cid, ([], []))
        if days and days[-1] == eff:
            prices[-1] = price
        else:
            days.append(eff)
            prices.append(price)
    return PriceBook(current, history)


async def set_price_from(db: AsyncSession, child: ChildProfile, price: float, effective_from: date,
                         discount_pct: Optional[int] = None, reason: Optional[str] = None) -> None:
    """Новая цена с даты. Если истории ещё нет — прежняя цена фиксируется как
    действовавшая раньше, чтобы прошлые занятия не пересчитались."""
    hist = (await db.execute(
        select(StudentPriceHistory).where(StudentPriceHistory.child_id == child.id)
    )).scalars().all()
    if not hist:
        earliest = await db.scalar(select(func.min(Lesson.date)).where(Lesson.child_id == child.id))
        base_from = min(earliest or effective_from, effective_from - timedelta(days=1))
        db.add(StudentPriceHistory(child_id=child.id, price=child.lesson_price or BASE_PRICE,
                                   effective_from=base_from, reason="Цена до изменения"))
    same = next((h for h in hist if h.effective_from == effective_from), None)
    if same:
        same.price, same.discount_pct, same.reason = price, discount_pct, reason
    else:
        db.add(StudentPriceHistory(child_id=child.id, price=price, effective_from=effective_from,
                                   discount_pct=discount_pct, reason=reason))
    await db.flush()
    if effective_from <= minsk_today():
        child.lesson_price = price


async def sync_current_prices(db: AsyncSession) -> int:
    """Каждую ночь: lesson_price = цена из истории, действующая сегодня."""
    book = await load_prices(db)
    if not book.history:
        return 0
    today = minsk_today()
    fixed = 0
    for child in (await db.execute(select(ChildProfile).where(ChildProfile.id.in_(list(book.history))))).scalars().all():
        p = book.price(child.id, today)
        if child.lesson_price != p:
            child.lesson_price = p
            fixed += 1
    if fixed:
        await db.commit()
    return fixed


async def discount_state(db: AsyncSession, child_ids: Iterable[int]) -> dict[int, dict]:
    """Скидка за рекомендации: какая уже применена и от какой базовой цены."""
    ids = list(set(child_ids))
    out: dict[int, dict] = {}
    if not ids:
        return out
    rows = (await db.execute(
        select(StudentPriceHistory.child_id, StudentPriceHistory.price, StudentPriceHistory.discount_pct,
               StudentPriceHistory.effective_from)
        .where(StudentPriceHistory.child_id.in_(ids))
        .order_by(StudentPriceHistory.child_id, StudentPriceHistory.effective_from, StudentPriceHistory.id)
    )).all()
    today = minsk_today()
    for cid, price, pct, eff in rows:
        st = out.setdefault(cid, {"applied": 0, "base": None})
        if eff > today:
            continue
        if pct:
            st["applied"] = pct
        else:
            st["applied"] = 0
            st["base"] = price
    return out


def discount_for_leads(leads: int) -> int:
    for need, pct in DISCOUNT_STEPS:
        if leads >= need:
            return pct
    return 0


def discount_offer(current_price: float, applied: int, base: Optional[float], leads: int) -> Optional[dict]:
    """Положена ли (бо́льшая) скидка: только при базовой цене 40 BYN."""
    base_price = base if (applied and base) else current_price
    if abs((base_price or 0) - BASE_PRICE) > 0.001:
        return None
    pct = discount_for_leads(leads)
    if pct <= applied:
        return None
    return {"pct": pct, "from": round(current_price, 2), "to": round(BASE_PRICE * (100 - pct) / 100, 2)}
