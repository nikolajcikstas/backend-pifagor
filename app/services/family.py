"""Семья родителя: все дети, которых он должен видеть в личном кабинете.

Раньше родитель видел только детей, напрямую привязанных к его аккаунту
(parent_children). Но каждому ученику при создании выдаётся своя пара кодов
«ученик + родитель», поэтому у мамы двоих детей обычно привязан только тот
ребёнок, по чьему коду она зарегистрировалась: второго ребёнка, его
расписание и общие оплаты она не видела.

Семья собирается по связям, которые проставляет администратор:
• прямая привязка родитель ↔ ребёнок;
• один плательщик на нескольких детей (чеки EasyPay, «семейные» оплаты);
• один договор на нескольких детей.
Размер семьи ограничен: если по плательщику набирается подозрительно много
детей (например, одинаковое имя у разных людей), связь по плательщику не
используется.
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import ParentChild, ParentContract, ParentContractChild, PayerChildLink

MAX_FAMILY = 6


async def _contract_children(db: AsyncSession, ids: set[int], parent_profile_id: int | None) -> set[int]:
    conds = [ParentContract.child_id.in_(ids)]
    if parent_profile_id:
        conds.append(ParentContract.parent_id == parent_profile_id)
    extra_ids = select(ParentContractChild.contract_id).where(ParentContractChild.child_id.in_(ids))
    conds.append(ParentContract.id.in_(extra_ids))
    contracts = (await db.execute(select(ParentContract.id, ParentContract.child_id).where(or_(*conds)))).all()
    out = {cid for _id, cid in contracts if cid}
    if contracts:
        out |= set((await db.execute(
            select(ParentContractChild.child_id).where(ParentContractChild.contract_id.in_([c[0] for c in contracts]))
        )).scalars().all())
    return out


async def _payer_children(db: AsyncSession, ids: set[int]) -> set[int]:
    keys = set((await db.execute(
        select(PayerChildLink.payer_name_normalized).where(PayerChildLink.child_id.in_(ids))
    )).scalars().all())
    if not keys:
        return set()
    return set((await db.execute(
        select(PayerChildLink.child_id).where(PayerChildLink.payer_name_normalized.in_(keys))
    )).scalars().all())


async def _closure(db: AsyncSession, fam: set[int]) -> set[int]:
    for _ in range(3):
        grown = set(fam)
        grown |= await _contract_children(db, grown, None)
        by_payer = await _payer_children(db, grown)
        if len(grown | by_payer) <= MAX_FAMILY:
            grown |= by_payer
        if grown == fam:
            break
        fam = grown
    return fam


async def family_child_ids(db: AsyncSession, parent_profile_id: int) -> list[int]:
    direct = set((await db.execute(
        select(ParentChild.child_id).where(ParentChild.parent_id == parent_profile_id)
    )).scalars().all())
    fam = set(direct) | await _contract_children(db, direct or {-1}, parent_profile_id)
    if not fam:
        return []
    return sorted(await _closure(db, fam))


async def family_parent_user_ids(db: AsyncSession, child_id: int) -> list[int]:
    """Аккаунты родителей, которые видят этого ребёнка (с учётом семьи) —
    кому слать уведомления о его отчётах."""
    from app.models.models import ParentProfile
    fam = await _closure(db, {child_id})
    rows = (await db.execute(
        select(ParentProfile.user_id).join(ParentChild, ParentChild.parent_id == ParentProfile.id)
        .where(ParentChild.child_id.in_(fam))
    )).scalars().all()
    return sorted(set(rows))


def parent_child_ids(user) -> list[int]:
    """Дети родителя для проверок доступа: семья, посчитанная при входе
    (get_current_user), иначе — только прямые привязки."""
    fam = getattr(user, "pf_family_ids", None)
    if fam is not None:
        return list(fam)
    pp = getattr(user, "parent_profile", None)
    return [pc.child_id for pc in pp.children] if pp else []
