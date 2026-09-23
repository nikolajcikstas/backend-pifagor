"""Общая логика финансового отчёта по ученику(ам) — сколько занятий
проведено, сколько оплачено (с учётом «семейных» платежей от одного
плательщика на нескольких детей). Используется и в админском дашборде
(/admin/finance-report), и в личном кабинете родителя (своя карточка
оплат по ребёнку) — чтобы цифры в обоих местах совпадали один в один."""

from datetime import date, timedelta
from typing import Iterable, List, Optional

from sqlalchemy import Date, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.models import ChildProfile, EmailReceipt, Lesson, LessonStatus, PayerChildLink
from app.schemas.schemas import StudentFinanceRow


async def compute_finance_rows(
    db: AsyncSession,
    week_start: Optional[date] = None,
    child_ids: Optional[Iterable[int]] = None,
) -> List[StudentFinanceRow]:
    """Считает lessons_conducted / lessons_paid / amount_paid / lesson_price
    по каждому ученику. Если week_start задан — только за эту неделю.
    Если child_ids задан — считает (внутри общей «семейной» логики) и
    возвращает только строки по этим ученикам."""
    week_end = week_start + timedelta(days=6) if week_start else None
    lesson_filters = [Lesson.status == LessonStatus.completed]
    receipt_filters = [EmailReceipt.child_id.isnot(None)]
    pooled_receipt_filters = []

    if week_start and week_end:
        lesson_filters.extend([
            Lesson.date >= week_start,
            Lesson.date <= week_end,
        ])
        receipt_filters.extend([
            cast(EmailReceipt.payment_date, Date) >= week_start,
            cast(EmailReceipt.payment_date, Date) <= week_end,
        ])
        pooled_receipt_filters.extend([
            cast(EmailReceipt.payment_date, Date) >= week_start,
            cast(EmailReceipt.payment_date, Date) <= week_end,
        ])

    # Если запрошены конкретные ученики (например, ЛК одного родителя) —
    # сначала расширяем набор до «семьи» (братья/сёстры на одном плательщике),
    # а потом фильтруем ВСЕ запросы этим набором в самой базе. Раньше здесь
    # всегда читались занятия и чеки ВСЕХ учеников системы, а фильтрация по
    # child_ids происходила только в самом конце — для ЛК одного родителя это
    # означало пересчёт по всей базе ради 1-2 строк, и чем больше учеников,
    # тем медленнее грузился личный кабинет.
    scoped_child_ids: Optional[set] = None
    if child_ids is not None:
        scoped_child_ids = set(child_ids)
        family_links_res = await db.execute(
            select(PayerChildLink.payer_name_normalized).where(
                PayerChildLink.child_id.in_(scoped_child_ids)
            )
        )
        payer_keys = {row[0] for row in family_links_res.all()}
        if payer_keys:
            expand_res = await db.execute(
                select(PayerChildLink.child_id).where(
                    PayerChildLink.payer_name_normalized.in_(payer_keys)
                )
            )
            scoped_child_ids |= {row[0] for row in expand_res.all()}
        lesson_filters.append(Lesson.child_id.in_(scoped_child_ids))
        receipt_filters.append(EmailReceipt.child_id.in_(scoped_child_ids))

    lessons_res = await db.execute(
        select(Lesson.child_id, Lesson.date).where(*lesson_filters)
    )
    lessons_by_child: dict[int, int] = {}
    lesson_dates_by_child: dict[int, list] = {}
    for cid, ldate in lessons_res.all():
        lessons_by_child[cid] = lessons_by_child.get(cid, 0) + 1
        lesson_dates_by_child.setdefault(cid, []).append(ldate)

    receipts_res = await db.execute(
        select(EmailReceipt.child_id, func.sum(EmailReceipt.amount).label("total"))
        .join(ChildProfile, EmailReceipt.child_id == ChildProfile.id)
        .where(
            *receipt_filters,
            or_(
                ChildProfile.accounting_start_date.is_(None),
                func.coalesce(EmailReceipt.payment_date, EmailReceipt.created_at) >= ChildProfile.accounting_start_date,
            ),
        )
        .group_by(EmailReceipt.child_id)
    )
    amounts_by_child = {row.child_id: row.total for row in receipts_res}

    links_query = select(PayerChildLink)
    if scoped_child_ids is not None:
        links_query = links_query.where(PayerChildLink.child_id.in_(scoped_child_ids))
    links_res = await db.execute(links_query)
    group_children: dict[str, set] = {}
    for link in links_res.scalars().all():
        group_children.setdefault(link.payer_name_normalized, set()).add(link.child_id)
    groups = {key: ids for key, ids in group_children.items() if len(ids) > 1}

    if groups:
        from app.services.email_parser import _normalize_name

        all_receipts_query = select(EmailReceipt)
        if pooled_receipt_filters:
            all_receipts_query = all_receipts_query.where(*pooled_receipt_filters)
        all_receipts_res = await db.execute(all_receipts_query)
        pooled_paid: dict[str, float] = {}
        for r in all_receipts_res.scalars().all():
            norm = _normalize_name(r.payer_name)
            if norm in groups:
                pooled_paid[norm] = pooled_paid.get(norm, 0.0) + r.amount

        group_child_ids = {cid for ids in groups.values() for cid in ids}
        prices_res = await db.execute(
            select(ChildProfile.id, ChildProfile.lesson_price).where(ChildProfile.id.in_(group_child_ids))
        )
        price_by_child = {row[0]: (row[1] or 40) for row in prices_res.all()}

        for group_key, child_ids_in_group in groups.items():
            combined = []
            for cid in child_ids_in_group:
                for ldate in lesson_dates_by_child.get(cid, []):
                    combined.append((ldate, cid))
            combined.sort(key=lambda pair: pair[0])

            remaining = pooled_paid.get(group_key, 0.0)
            paid_for_child: dict[int, float] = {cid: 0.0 for cid in child_ids_in_group}
            for _ldate, cid in combined:
                price = price_by_child.get(cid, 40)
                if remaining + 1e-9 >= price:
                    paid_for_child[cid] += price
                    remaining -= price
                else:
                    break

            for cid in child_ids_in_group:
                amounts_by_child[cid] = paid_for_child[cid]

    all_child_ids = set(lessons_by_child) | set(amounts_by_child)
    if child_ids is not None:
        all_child_ids &= set(child_ids)

    if not all_child_ids:
        return []

    cp_res = await db.execute(
        select(ChildProfile)
        .options(joinedload(ChildProfile.user))
        .where(ChildProfile.id.in_(all_child_ids))
    )
    children = {cp.id: cp for cp in cp_res.scalars().unique()}

    rows: List[StudentFinanceRow] = []
    for child_id in sorted(all_child_ids):
        cp = children.get(child_id)
        if not cp or not cp.user:
            continue
        u = cp.user
        conducted = lessons_by_child.get(child_id, 0)
        amount_paid = amounts_by_child.get(child_id, 0.0) or 0.0
        lesson_price = cp.lesson_price or 40
        lessons_paid = int(amount_paid // lesson_price) if lesson_price else 0

        rows.append(StudentFinanceRow(
            child_id=child_id,
            student_name=f"{u.last_name} {u.first_name}".strip(),
            lessons_conducted=conducted,
            lessons_paid=lessons_paid,
            amount_paid=round(amount_paid, 2),
            lesson_price=round(lesson_price, 2),
        ))

    return rows
