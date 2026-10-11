"""
Расчёт заработка репетитора с учётом истории изменений ставки.

Если у репетитора никогда не меняли ставку через "историю" — используется
текущая TutorProfile.rate_per_hour для всех занятий (как раньше, обратная
совместимость). Если ставка менялась с конкретной даты — для каждого
занятия берётся та ставка, что действовала на дату этого занятия, а не
текущая.
"""
from datetime import date as DateType, datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Lesson, LessonStatus, TutorProfile, TutorRateHistory


async def compute_tutor_earnings(
    db: AsyncSession,
    tutor_id: int,
    as_of_date: Optional[DateType] = None,
) -> tuple[float, int]:
    """Возвращает (сумма_заработка, кол-во_проведённых_занятий) по всем
    занятиям со статусом "проведено" (до as_of_date включительно, либо за
    всё время, если as_of_date не указан)."""
    history_res = await db.execute(
        select(TutorRateHistory)
        .where(TutorRateHistory.tutor_id == tutor_id)
        .order_by(TutorRateHistory.effective_from)
    )
    history = history_res.scalars().all()

    tutor_res = await db.execute(select(TutorProfile).where(TutorProfile.id == tutor_id))
    tutor = tutor_res.scalar_one_or_none()
    flat_rate = (tutor.rate_per_hour or 0) if tutor else 0

    lesson_filters = [Lesson.tutor_id == tutor_id, Lesson.status == LessonStatus.completed]
    if as_of_date is not None:
        lesson_filters.append(Lesson.date <= as_of_date)
    lessons_res = await db.execute(select(Lesson.date).where(*lesson_filters))
    lesson_dates = [row[0] for row in lessons_res.all()]

    def rate_for_date(d: DateType) -> float:
        if not history:
            return flat_rate
        # До самой ранней записи в истории — действует ставка из этой самой
        # ранней записи (считаем, что она была в силе "всегда до этого").
        applicable = history[0].rate_per_hour
        for h in history:
            if h.effective_from <= d:
                applicable = h.rate_per_hour
            else:
                break
        return applicable

    total = sum(rate_for_date(d) for d in lesson_dates)
    return round(total, 2), len(lesson_dates)


def _rate_on(history: list, flat_rate: float, d: DateType) -> float:
    """Ставка на дату: та же логика, что в compute_tutor_earnings."""
    if not history:
        return flat_rate
    applicable = history[0][1]
    for eff, rate in history:
        if eff <= d:
            applicable = rate
        else:
            break
    return applicable


async def load_rate_histories(db: AsyncSession, tutor_ids: list) -> dict:
    """История ставок сразу для многих репетиторов одним запросом:
    {tutor_id: [(effective_from, rate), ...]} по возрастанию даты."""
    out: dict = {}
    if not tutor_ids:
        return out
    for tid, eff, rate in (await db.execute(
        select(TutorRateHistory.tutor_id, TutorRateHistory.effective_from, TutorRateHistory.rate_per_hour)
        .where(TutorRateHistory.tutor_id.in_(tutor_ids))
        .order_by(TutorRateHistory.tutor_id, TutorRateHistory.effective_from, TutorRateHistory.id)
    )).all():
        out.setdefault(tid, []).append((eff, rate))
    return out


async def compute_earnings_bulk(
    db: AsyncSession,
    tutor_ids: list,
    as_of_date: Optional[DateType] = None,
    histories: Optional[dict] = None,
) -> dict:
    """То же, что compute_tutor_earnings, но для всех репетиторов сразу —
    три запроса вместо трёх на каждого (на сервере это давало долгую загрузку
    вкладки «Репетиторы»). Возвращает {tutor_id: заработано}."""
    if not tutor_ids:
        return {}
    from sqlalchemy import func as _f
    if histories is None:
        histories = await load_rate_histories(db, tutor_ids)
    flat = {tid: (rate or 0) for tid, rate in (await db.execute(
        select(TutorProfile.id, TutorProfile.rate_per_hour).where(TutorProfile.id.in_(tutor_ids))
    )).all()}
    filters = [Lesson.tutor_id.in_(tutor_ids), Lesson.status == LessonStatus.completed]
    if as_of_date is not None:
        filters.append(Lesson.date <= as_of_date)
    totals = {tid: 0.0 for tid in tutor_ids}
    for tid, d, n in (await db.execute(
        select(Lesson.tutor_id, Lesson.date, _f.count()).where(*filters).group_by(Lesson.tutor_id, Lesson.date)
    )).all():
        totals[tid] = totals.get(tid, 0.0) + _rate_on(histories.get(tid) or [], flat.get(tid, 0), d) * n
    return {tid: round(v, 2) for tid, v in totals.items()}


def current_and_pending_from_history(history: list, fallback_rate: Optional[float]):
    """Как get_current_and_pending_rate, но по уже загруженной истории."""
    if not history:
        return fallback_rate, None, None
    today = minsk_today()
    current = pending = pending_from = None
    for eff, rate in history:
        if eff <= today:
            current = rate
        elif pending is None:
            pending, pending_from = rate, eff
    if current is None:
        current = history[0][1]
    return current, pending, pending_from


async def get_current_and_pending_rate(
    db: AsyncSession,
    tutor_id: int,
    fallback_rate: Optional[float] = None,
) -> tuple[Optional[float], Optional[float], Optional[DateType]]:
    """Возвращает (ставка_действующая_сегодня, ставка_запланированная_на_будущее,
    дата_с_которой_она_вступит). Если запланированной ставки нет —
    вторые два значения будут None."""
    history_res = await db.execute(
        select(TutorRateHistory)
        .where(TutorRateHistory.tutor_id == tutor_id)
        .order_by(TutorRateHistory.effective_from)
    )
    history = history_res.scalars().all()
    if not history:
        return fallback_rate, None, None

    today = minsk_today()
    current = None
    pending = None
    pending_from = None
    for h in history:
        if h.effective_from <= today:
            current = h.rate_per_hour
        elif pending is None:
            pending = h.rate_per_hour
            pending_from = h.effective_from

    if current is None:
        current = history[0].rate_per_hour

    return current, pending, pending_from


def minsk_today() -> DateType:
    """Сегодня по Минску (сервер работает в UTC — после полуночи по Минску
    ещё «вчера» по UTC, из-за этого запланированная ставка включалась позже)."""
    return (datetime.utcnow() + timedelta(hours=3)).date()


async def sync_tutor_rates(db: AsyncSession) -> int:
    """Поле «ставка» у репетитора = ставка из истории, действующая сегодня.
    Раньше поле обновлялось только в момент сохранения: запланированная на
    01.10 ставка в этот день «исчезала» из запланированных, а в карточке
    оставалась старая. Возвращает число исправленных репетиторов."""
    rows = (await db.execute(
        select(TutorRateHistory.tutor_id, TutorRateHistory.rate_per_hour, TutorRateHistory.effective_from)
        .order_by(TutorRateHistory.tutor_id, TutorRateHistory.effective_from, TutorRateHistory.id)
    )).all()
    if not rows:
        return 0
    today = minsk_today()
    current: dict[int, float] = {}
    for tid, rate, eff in rows:
        if tid not in current or eff <= today:
            current[tid] = rate
    fixed = 0
    for tutor in (await db.execute(select(TutorProfile).where(TutorProfile.id.in_(list(current))))).scalars().all():
        if tutor.rate_per_hour != current[tutor.id]:
            tutor.rate_per_hour = current[tutor.id]
            fixed += 1
    if fixed:
        await db.commit()
    return fixed


async def set_tutor_rate_from(db: AsyncSession, tutor: TutorProfile, rate: float, effective_from: DateType) -> None:
    """Записать ставку в историю с даты (если на эту дату уже есть запись —
    заменить). Если истории ещё нет — сначала фиксируется прежняя ставка
    как действовавшая с первого занятия, чтобы прошлое не пересчиталось."""
    hist = (await db.execute(
        select(TutorRateHistory).where(TutorRateHistory.tutor_id == tutor.id)
    )).scalars().all()
    if not hist and tutor.rate_per_hour:
        from sqlalchemy import func as _f
        earliest = await db.scalar(select(_f.min(Lesson.date)).where(Lesson.tutor_id == tutor.id))
        base_from = min(earliest or effective_from, effective_from - timedelta(days=1))
        db.add(TutorRateHistory(tutor_id=tutor.id, rate_per_hour=tutor.rate_per_hour, effective_from=base_from))
    same = next((h for h in hist if h.effective_from == effective_from), None)
    if same:
        same.rate_per_hour = rate
    else:
        db.add(TutorRateHistory(tutor_id=tutor.id, rate_per_hour=rate, effective_from=effective_from))
    await db.flush()
