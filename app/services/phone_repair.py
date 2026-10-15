"""Восстановление телефонов, затёртых заглушкой +375000000000.

Раньше форма регистрации в личном кабинете присылала вместо телефона
заглушку +375000000000, и при регистрации родителя по коду она затирала
номер, который администратор ввёл в CRM. Исправлено в auth.register, а уже
затёртые номера восстанавливаются здесь (при каждом запуске сервера, только
там, где настоящего номера нет):
• родителю — номер из его договора (договор этого родителя или его детей,
  самый свежий);
• если договора с номером нет — заглушка убирается (поле остаётся пустым,
  его можно заполнить в CRM).
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import or_, select

logger = logging.getLogger(__name__)


def real_phone(p: Optional[str]) -> bool:
    digits = "".join(ch for ch in (p or "") if ch.isdigit())
    return len(digits) >= 9 and not set(digits.removeprefix("375")) <= {"0"}


def is_zero_placeholder(p: Optional[str]) -> bool:
    digits = "".join(ch for ch in (p or "") if ch.isdigit())
    return bool(digits) and set(digits.removeprefix("375")) <= {"0"}


def _fmt(p: Optional[str]) -> Optional[str]:
    digits = "".join(ch for ch in (p or "") if ch.isdigit())
    if len(digits) < 9:
        return None
    d9 = digits[-9:]
    if set(d9) <= {"0"}:
        return None
    return "+375" + d9


async def repair_phones(db) -> dict:
    from app.models.models import (
        ParentChild, ParentContract, ParentContractChild, ParentProfile, RoleEnum, User,
    )
    out = {"from_contract": 0, "cleared": 0}
    parents = (await db.execute(
        select(ParentProfile.id, User).join(User, User.id == ParentProfile.user_id)
        .where(User.role == RoleEnum.parent)
    )).all()
    for pp_id, user in parents:
        if real_phone(user.phone):
            continue
        kids = set((await db.execute(
            select(ParentChild.child_id).where(ParentChild.parent_id == pp_id)
        )).scalars().all())
        conds = [ParentContract.parent_id == pp_id]
        if kids:
            conds += [ParentContract.child_id.in_(kids),
                      ParentContract.id.in_(select(ParentContractChild.contract_id)
                                            .where(ParentContractChild.child_id.in_(kids)))]
        contracts = (await db.execute(
            select(ParentContract.parent_phone, ParentContract.start_date, ParentContract.created_at)
            .where(or_(*conds), ParentContract.parent_phone.isnot(None))
        )).all()
        best = None
        for phone, start, created in sorted(
                contracts, key=lambda r: (r[1] is not None, r[1], r[2] is not None, r[2]), reverse=True):
            best = _fmt(phone)
            if best:
                break
        if best:
            user.phone = best
            out["from_contract"] += 1
        elif is_zero_placeholder(user.phone):
            user.phone = None
            out["cleared"] += 1
    # у учеников заглушку тоже убираем — настоящий номер можно ввести в CRM
    kids_bad = (await db.execute(
        select(User).where(User.role == RoleEnum.child, User.phone.isnot(None))
    )).scalars().all()
    for u in kids_bad:
        if is_zero_placeholder(u.phone):
            u.phone = None
            out["cleared"] += 1
    await db.commit()
    return out


async def phone_repair_task():
    import asyncio
    from app.db.session import AsyncSessionLocal
    await asyncio.sleep(20)
    try:
        async with AsyncSessionLocal() as db:
            res = await repair_phones(db)
        if res["from_contract"] or res["cleared"]:
            logger.info("phone repair: %s", res)
    except Exception:
        logger.exception("phone repair failed")
