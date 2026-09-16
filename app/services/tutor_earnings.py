"""
Расчёт заработка репетитора с учётом истории изменений ставки.

Если у репетитора никогда не меняли ставку через "историю" — используется
текущая TutorProfile.rate_per_hour для всех занятий (как раньше, обратная
совместимость). Если ставка менялась с конкретной даты — для каждого
занятия берётся та ставка, что действовала на дату этого занятия, а не
текущая.
"""
from datetime import date as DateType
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
