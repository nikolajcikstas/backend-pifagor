"""Напоминания родителям о задолженности — веб-уведомлением на телефон.

• Задолженность считается так же, как в личном кабинете родителя («Оплаты»):
  итог по всей семье (все дети родителя).
• Если задолженность больше 160 р — уведомление приходит сразу (в дневное
  время), а дальше повторяется каждые 2 дня, пока долг не станет 160 р или меньше.
• Как только долг погашен — отметка сбрасывается: при новом долге первое
  напоминание снова придёт сразу.
• Только родители, включившие уведомления в личном кабинете. Ночью не шлём:
  проверка идёт с 10:00 до 20:00 по Минску.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import delete, select

logger = logging.getLogger(__name__)

THRESHOLD = 160.0      # р — напоминаем, если долг больше этой суммы
EVERY_DAYS = 2         # повтор, пока долг не погашен
DAY_HOURS = (10, 20)   # с 10:00 до 20:00 по Минску
CHECK_EVERY_SEC = 1800
MINSK = timedelta(hours=3)


def _money(x: float) -> str:
    s = f"{x:,.2f}".replace(",", " ").replace(".", ",")
    return s[:-3] if s.endswith(",00") else s


def reminder_text(debt: float) -> tuple[str, str]:
    return ("Пифагор: оплата занятий",
            f"Задолженность по оплате занятий — {_money(debt)} р. Пожалуйста, внесите оплату. "
            "Подробности — в личном кабинете, раздел «Оплаты».")


async def run_once(now_utc: Optional[datetime] = None) -> dict:
    from app.db.session import AsyncSessionLocal
    from app.models.models import AppState, ParentProfile, PushSubscription, RoleEnum, User
    from app.services.family import family_child_ids
    from app.services.finance_report import compute_finance_rows
    from app.services import webpush

    now_utc = now_utc or datetime.utcnow()
    local = now_utc + MINSK
    out = {"checked": 0, "sent": 0, "reset": 0}
    if not (DAY_HOURS[0] <= local.hour < DAY_HOURS[1]):
        return out
    today = local.date()
    async with AsyncSessionLocal() as db:
        subs = (await db.execute(
            select(PushSubscription).join(User, User.id == PushSubscription.user_id)
            .where(User.role == RoleEnum.parent, User.is_active.isnot(False))
        )).scalars().all()
        by_user: dict[int, list] = {}
        for s in subs:
            by_user.setdefault(s.user_id, []).append(s)
        for uid, user_subs in by_user.items():
            try:
                pp_id = await db.scalar(select(ParentProfile.id).where(ParentProfile.user_id == uid))
                if not pp_id:
                    continue
                kids = await family_child_ids(db, pp_id)
                debt = round(sum(r.debt for r in await compute_finance_rows(db, child_ids=kids)), 2) if kids else 0.0
                out["checked"] += 1
                key = f"debt_push:{uid}"
                mark = await db.get(AppState, key)
                if debt <= THRESHOLD:
                    if mark is not None:
                        await db.execute(delete(AppState).where(AppState.key == key))
                        await db.commit()
                        out["reset"] += 1
                    continue
                if mark is not None:
                    try:
                        last = date.fromisoformat(mark.value[:10])
                    except ValueError:
                        last = None
                    if last and (today - last).days < EVERY_DAYS:
                        continue
                title, body = reminder_text(debt)
                res = await webpush.send_to(db, user_subs, title, body, "/", "debt")
                if res.get("sent"):
                    mark = await db.get(AppState, key)
                    if mark is None:
                        db.add(AppState(key=key, value=today.isoformat(), updated_at=datetime.utcnow()))
                    else:
                        mark.value, mark.updated_at = today.isoformat(), datetime.utcnow()
                    await db.commit()
                    out["sent"] += 1
            except Exception:
                await db.rollback()
                logger.exception("debt reminder failed for user %s", uid)
    return out


async def debt_reminder_task():
    await asyncio.sleep(240)  # сервер «просыпается», когда человек входит в кабинет — сначала вход
    while True:
        try:
            res = await run_once()
            if res["sent"] or res["reset"]:
                logger.info("debt reminders: %s", res)
        except Exception:
            logger.exception("debt reminders failed")
        await asyncio.sleep(CHECK_EVERY_SEC)
