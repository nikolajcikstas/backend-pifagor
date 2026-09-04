"""
Разовый скрипт-исправление для выплат, сделанных до того, как расчёт суммы
привязали к выбранной дате оплаты (раньше "Оплатить" списывал весь текущий
баланс на сегодня, а должен был — только заработанное по выбранную дату
включительно).

Пересчитывает amount у каждой уже существующей записи в tutor_payouts,
в хронологическом порядке (как они были созданы), используя ту же дату
paid_at, что вы уже выбрали при оплате, но правильную формулу:
    сумма = (проведённые занятия с датой <= paid_at) * ставка - то, что
            уже было списано предыдущими (тоже пересчитанными) выплатами
             этого же репетитора

Запускать один раз:
    python fix_payouts.py

Безопасно запускать повторно — результат идемпотентен (пересчёт от
исходных данных о занятиях, а не от предыдущего пересчёта).
"""
import asyncio

from sqlalchemy import select, func

from app.db.session import AsyncSessionLocal
from app.models.models import TutorProfile, TutorPayout, Lesson, LessonStatus


async def fix_payouts():
    async with AsyncSessionLocal() as db:
        tutors_result = await db.execute(select(TutorProfile))
        tutors = tutors_result.scalars().all()

        total_changed = 0
        for tutor in tutors:
            payouts_result = await db.execute(
                select(TutorPayout)
                .where(TutorPayout.tutor_id == tutor.id)
                .order_by(TutorPayout.created_at, TutorPayout.id)
            )
            payouts = payouts_result.scalars().all()
            if not payouts:
                continue

            rate = tutor.rate_per_hour or 0
            running_paid = 0.0

            for payout in payouts:
                count_result = await db.execute(
                    select(func.count(Lesson.id)).where(
                        Lesson.tutor_id == tutor.id,
                        Lesson.status == LessonStatus.completed,
                        Lesson.date <= payout.paid_at,
                    )
                )
                lessons_count = count_result.scalar() or 0
                earned_by_date = round(lessons_count * rate, 2) if rate else lessons_count * 80
                correct_amount = round(max(0.0, earned_by_date - running_paid), 2)

                if correct_amount != payout.amount:
                    print(
                        f"tutor #{tutor.id}: payout #{payout.id} "
                        f"(paid_at={payout.paid_at}) {payout.amount} -> {correct_amount}"
                    )
                    payout.amount = correct_amount
                    total_changed += 1

                running_paid += correct_amount

        await db.commit()
        print(f"Готово. Исправлено выплат: {total_changed}")


if __name__ == "__main__":
    asyncio.run(fix_payouts())
