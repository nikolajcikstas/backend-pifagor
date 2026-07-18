"""
Seed script — запускать один раз:
  python seed.py
Создаёт: subjects, prices, FAQ, admin user, demo tutor/parent/child
"""
import asyncio
from app.db.session import AsyncSessionLocal, engine, Base
from app.models.models import (
    User, Subject, Price, FAQ, TutorProfile, ParentProfile, ChildProfile,
    TutorSubject, RoleEnum
)
from app.core.security import get_password_hash


SUBJECTS = [
    {"name": "Математика", "slug": "matematika", "icon": "📐"},
    {"name": "Физика", "slug": "fizika", "icon": "⚡"},
    {"name": "Химия", "slug": "himiya", "icon": "🧪"},
    {"name": "Русский язык", "slug": "russkiy", "icon": "📝"},
    {"name": "Английский язык", "slug": "angliyskiy", "icon": "🇬🇧"},
    {"name": "Биология", "slug": "biologiya", "icon": "🌿"},
    {"name": "История", "slug": "istoriya", "icon": "📚"},
    {"name": "Информатика", "slug": "informatika", "icon": "💻"},
]

FAQS = [
    {"question": "Как проходит первое бесплатное занятие?", "answer": "Первое занятие длится 45 минут. Репетитор знакомится с учеником, определяет уровень знаний и составляет план обучения. Никаких обязательств — только после первого занятия вы решаете, продолжать ли обучение."},
    {"question": "Как выбрать репетитора?", "answer": "На нашем сайте вы можете ознакомиться с профилями всех репетиторов — их образованием, опытом и отзывами родителей. Также можно записаться на пробное занятие с несколькими репетиторами."},
    {"question": "Как проводятся занятия?", "answer": "Занятия проводятся онлайн через видеосвязь. Репетитор использует интерактивную доску, делится экраном и материалами. Это так же эффективно, как занятия очно."},
    {"question": "Что входит в стоимость?", "answer": "В стоимость занятия входят: подготовка материалов, домашнее задание, отчёт родителям каждые 5 занятий и поддержка между занятиями."},
    {"question": "Можно ли перенести или отменить занятие?", "answer": "Да. Занятие можно перенести или отменить не позднее чем за 24 часа. При отмене менее чем за 24 часа занятие считается проведённым."},
]


async def seed():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        # Subjects
        subjects = {}
        for s in SUBJECTS:
            obj = Subject(**s)
            db.add(obj)
            await db.flush()
            subjects[s["slug"]] = obj

        # Prices
        prices = [
            Price(title="1 занятие", price_per_lesson=1500, lessons_in_package=1),
            Price(title="8 занятий", price_per_lesson=1350, lessons_in_package=8, discount_percent=10),
            Price(title="16 занятий", price_per_lesson=1200, lessons_in_package=16, discount_percent=20),
        ]
        for p in prices:
            db.add(p)

        # FAQs
        for i, f in enumerate(FAQS):
            db.add(FAQ(**f, order=i))

        # Admin
        admin = User(
            email="admin@pifagor.ru",
            hashed_password=get_password_hash("admin123"),
            role=RoleEnum.admin,
            first_name="Администратор",
            last_name="Пифагор",
        )
        db.add(admin)

        # Demo tutor
        tutor_user = User(
            email="tutor@pifagor.ru",
            hashed_password=get_password_hash("tutor123"),
            role=RoleEnum.tutor,
            first_name="Иван",
            last_name="Будько",
            phone="+7 999 123-45-67",
        )
        db.add(tutor_user)
        await db.flush()

        tutor_profile = TutorProfile(
            user_id=tutor_user.id,
            bio="Опытный преподаватель математики и физики. Готовлю к ОГЭ и ЕГЭ.",
            education="МГУ, механико-математический факультет",
            experience_years=7,
            rate_per_hour=1500,
            is_published=True,
        )
        db.add(tutor_profile)
        await db.flush()

        db.add(TutorSubject(tutor_id=tutor_profile.id, subject_id=subjects["matematika"].id))
        db.add(TutorSubject(tutor_id=tutor_profile.id, subject_id=subjects["fizika"].id))

        # Demo parent
        parent_user = User(
            email="parent@pifagor.ru",
            hashed_password=get_password_hash("parent123"),
            role=RoleEnum.parent,
            first_name="Надежда",
            last_name="Кадышева",
            phone="+7 999 765-43-21",
        )
        db.add(parent_user)
        await db.flush()

        parent_profile = ParentProfile(user_id=parent_user.id)
        db.add(parent_profile)

        # Demo child
        child_user = User(
            email="child@pifagor.ru",
            hashed_password=get_password_hash("child123"),
            role=RoleEnum.child,
            first_name="Алексей",
            last_name="Кадышев",
        )
        db.add(child_user)
        await db.flush()

        child_profile = ChildProfile(user_id=child_user.id, grade=10)
        db.add(child_profile)

        await db.commit()
        print("✅ Seed complete!")
        print("   admin@pifagor.ru / admin123")
        print("   tutor@pifagor.ru / tutor123")
        print("   parent@pifagor.ru / parent123")
        print("   child@pifagor.ru / child123")


if __name__ == "__main__":
    asyncio.run(seed())
