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
from app.services.pricing import load_prices, minsk_today


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
        # семья может платить с разных имён (мама, папа, старший ребёнок) —
        # собираем всех детей и всех плательщиков семьи, пока набор растёт
        for _ in range(3):
            if not payer_keys:
                break
            expand_res = await db.execute(
                select(PayerChildLink.child_id).where(
                    PayerChildLink.payer_name_normalized.in_(payer_keys)
                )
            )
            grown = scoped_child_ids | {row[0] for row in expand_res.all()}
            more_keys = {row[0] for row in (await db.execute(
                select(PayerChildLink.payer_name_normalized).where(PayerChildLink.child_id.in_(grown))
            )).all()}
            if grown == scoped_child_ids and more_keys == payer_keys:
                break
            scoped_child_ids, payer_keys = grown, more_keys
        lesson_filters.append(Lesson.child_id.in_(scoped_child_ids))
        receipt_filters.append(EmailReceipt.child_id.in_(scoped_child_ids))

    lessons_res = await db.execute(
        select(Lesson.child_id, Lesson.date, Lesson.paid_bank).where(*lesson_filters)
    )
    lessons_by_child: dict[int, int] = {}
    lesson_dates_by_child: dict[int, list] = {}
    bank_dates_by_child: dict[int, list] = {}   # оплачено на расчётный счёт (чека нет)
    for cid, ldate, paid_bank in lessons_res.all():
        lessons_by_child[cid] = lessons_by_child.get(cid, 0) + 1
        lesson_dates_by_child.setdefault(cid, []).append(ldate)
        if paid_bank:
            bank_dates_by_child.setdefault(cid, []).append(ldate)

    receipts_res = await db.execute(
        select(EmailReceipt.child_id, EmailReceipt.payer_name, EmailReceipt.amount)
        .join(ChildProfile, EmailReceipt.child_id == ChildProfile.id)
        .where(
            *receipt_filters,
            or_(
                ChildProfile.accounting_start_date.is_(None),
                func.coalesce(EmailReceipt.payment_date, EmailReceipt.created_at) >= ChildProfile.accounting_start_date,
            ),
        )
    )
    amounts_by_child: dict[int, float] = {}
    direct_by_child: dict[int, list] = {}   # (плательщик, сумма) — для семейного пересчёта ниже
    for cid, payer, amount in receipts_res.all():
        amounts_by_child[cid] = amounts_by_child.get(cid, 0.0) + (amount or 0.0)
        direct_by_child.setdefault(cid, []).append((payer, amount or 0.0))

    # занятия «оплачено на р/с» засчитываются как оплаченные по цене на их дату
    bank_paid: dict[int, float] = {}
    if bank_dates_by_child:
        bbook = await load_prices(db, set(bank_dates_by_child))
        for cid, dates in bank_dates_by_child.items():
            bank_paid[cid] = sum(bbook.price(cid, d) for d in dates)
            amounts_by_child[cid] = amounts_by_child.get(cid, 0.0) + bank_paid[cid]

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

        # Плательщики с общими детьми — одна семья. Раньше каждая группа
        # считалась отдельно и перезаписывала результат предыдущей: если
        # семья платила с двух имён (например, мама и старшая дочь), деньги
        # одного из плательщиков терялись. Теперь все платежи семьи — в одном
        # «котле» и распределяются по занятиям всех её детей по датам.
        clusters: list[tuple[set, set]] = [({key}, set(ids)) for key, ids in groups.items()]  # (плательщики, дети)
        merged = True
        while merged:
            merged = False
            for i in range(len(clusters)):
                for j in range(i + 1, len(clusters)):
                    if clusters[i][1] & clusters[j][1]:
                        clusters[i] = (clusters[i][0] | clusters[j][0], clusters[i][1] | clusters[j][1])
                        del clusters[j]
                        merged = True
                        break
                if merged:
                    break

        all_receipts_query = select(EmailReceipt)
        if pooled_receipt_filters:
            all_receipts_query = all_receipts_query.where(*pooled_receipt_filters)
        all_receipts_res = await db.execute(all_receipts_query)
        key_cluster = {k: i for i, (ck, _cc) in enumerate(clusters) for k in ck}
        pooled_paid: dict[int, float] = {}
        for r in all_receipts_res.scalars().all():
            ci = key_cluster.get(_normalize_name(r.payer_name))
            if ci is not None:
                pooled_paid[ci] = pooled_paid.get(ci, 0.0) + r.amount
        # чеки, привязанные напрямую к детям семьи от «посторонних» плательщиков
        # (не из семейных групп) — тоже деньги семьи, иначе они бы пропали
        for ci, (ck, cc) in enumerate(clusters):
            for cid in cc:
                for payer, amount in direct_by_child.get(cid, []):
                    if _normalize_name(payer) not in ck:
                        pooled_paid[ci] = pooled_paid.get(ci, 0.0) + amount

        group_child_ids = {cid for _ck, cc in clusters for cid in cc}
        gbook = await load_prices(db, group_child_ids)

        for ci, (_keys, child_ids_in_group) in enumerate(clusters):
            combined = []
            for cid in child_ids_in_group:
                bank_left = list(bank_dates_by_child.get(cid, []))
                for ldate in lesson_dates_by_child.get(cid, []):
                    if ldate in bank_left:   # уже оплачено на р/с — деньги семьи на него не тратим
                        bank_left.remove(ldate)
                        continue
                    combined.append((ldate, cid))
            combined.sort(key=lambda pair: pair[0])

            remaining = pooled_paid.get(ci, 0.0)
            paid_for_child: dict[int, float] = {cid: bank_paid.get(cid, 0.0) for cid in child_ids_in_group}
            for _ldate, cid in combined:
                price = gbook.price(cid, _ldate)
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
    book = await load_prices(db, all_child_ids)
    today = minsk_today()

    rows: List[StudentFinanceRow] = []
    for child_id in sorted(all_child_ids):
        cp = children.get(child_id)
        if not cp or not cp.user:
            continue
        u = cp.user
        conducted = lessons_by_child.get(child_id, 0)
        amount_paid = amounts_by_child.get(child_id, 0.0) or 0.0
        if book.has_history(child_id):
            # цена менялась с даты — каждое занятие по цене на его дату
            lesson_price = book.price(child_id, today)
            dates = sorted(lesson_dates_by_child.get(child_id, []))
            cost = sum(book.price(child_id, d) for d in dates)
            remaining, lessons_paid = amount_paid, 0
            for d in dates:
                p = book.price(child_id, d)
                if remaining + 1e-9 >= p:
                    remaining -= p
                    lessons_paid += 1
                else:
                    remaining = -1
                    break
            if remaining >= 0 and lesson_price:
                lessons_paid += int((remaining + 1e-9) // lesson_price)
        else:
            lesson_price = cp.lesson_price or 40
            cost = conducted * lesson_price
            lessons_paid = int(amount_paid // lesson_price) if lesson_price else 0

        rows.append(StudentFinanceRow(
            child_id=child_id,
            student_name=f"{u.last_name} {u.first_name}".strip(),
            lessons_conducted=conducted,
            lessons_paid=lessons_paid,
            amount_paid=round(amount_paid, 2),
            lesson_price=round(lesson_price, 2),
            cost_conducted=round(cost, 2),
            debt=round(max(0.0, cost - amount_paid), 2),
        ))

    return rows
