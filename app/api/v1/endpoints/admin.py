import secrets
from typing import List, Optional
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, cast, Date
from sqlalchemy.orm import joinedload
from pydantic import BaseModel  # 🌟 Добавили для Pydantic схемы

from app.db.session import get_db
from app.core.deps import require_admin
from app.models.models import (
    InviteCode, RoleEnum, Lesson, LessonStatus, Notification,
    ChildProfile, User, EmailReceipt, ParentProfile, ParentChild,  # 🌟 Добавили профили
    TutorProfile, TutorSubject, Subject, TutorDocument, TutorContract, Act,
    Homework, Report, Material, ParentContract, Payment, Comment, TestResult,
)
from app.schemas.schemas import (
    InviteCodeCreate, InviteCodeResponse,
    EmailReceiptOut, StudentFinanceRow,
    TutorProfileOut, AdminTutorUpdate,
    TutorDocumentCreate, TutorDocumentOut,
    ActOut,
)

router = APIRouter()


# Pydantic схема для ручного бинда
class BaseParentChildLink(BaseModel):
    parent_id: int
    child_id: int


class AdminStudentUpdate(BaseModel):
    lesson_price: Optional[float] = None


def generate_random_code(prefix: str) -> str:
    return f"PIF-{prefix.upper()}-{secrets.token_hex(3).upper()}"


async def _delete_invite_codes_for_user(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(select(InviteCode).where(InviteCode.used_by_user_id == user_id))
    direct_codes = result.scalars().all()
    code_ids = {code.id for code in direct_codes}
    code_ids.update(code.linked_code_id for code in direct_codes if code.linked_code_id)

    if not code_ids:
        return 0

    related = await db.execute(
        select(InviteCode).where(
            (InviteCode.id.in_(code_ids)) | (InviteCode.linked_code_id.in_(code_ids))
        )
    )
    codes = related.scalars().all()
    if not codes:
        return 0

    for code in codes:
        code.linked_code_id = None
    await db.flush()

    for code in codes:
        await db.delete(code)

    return len(codes)


@router.post("/invite-codes", response_model=List[InviteCodeResponse])
async def create_invite_codes(payload: InviteCodeCreate, db: AsyncSession = Depends(get_db)):
    role_str = str(payload.role).strip().lower()

    if role_str in ["pair", "student_parent"]:
        child_code = generate_random_code("CHD")
        child_invite = InviteCode(role=RoleEnum.child, code=child_code, description=payload.description)
        db.add(child_invite)
        await db.flush()

        parent_code = generate_random_code("PRN")
        parent_invite = InviteCode(
            role=RoleEnum.parent,
            code=parent_code,
            description=payload.description,
            linked_code_id=child_invite.id
        )
        db.add(parent_invite)
        await db.commit()
        return [child_invite, parent_invite]

    if role_str == "tutor":
        code_str = generate_random_code("TUT")
        invite = InviteCode(role=RoleEnum.tutor, code=code_str, description=payload.description)
        db.add(invite)
        await db.commit()
        return [invite]

    raise HTTPException(status_code=400, detail=f"Неверная роль для генерации кода: {payload.role}")


@router.get("/invite-codes", response_model=List[InviteCodeResponse])
async def list_invite_codes(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(InviteCode).order_by(InviteCode.created_at.desc()))
    return result.scalars().all()


@router.delete("/invite-codes", dependencies=[Depends(require_admin)])
async def clear_invite_codes(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(InviteCode))
    codes = result.scalars().all()

    for code in codes:
        code.linked_code_id = None
    await db.flush()

    for code in codes:
        await db.delete(code)

    await db.commit()
    return {"deleted": len(codes)}


# ─── Email Receipts ────────────────────────────────────────────────────────────

@router.get("/receipts", response_model=List[EmailReceiptOut], dependencies=[Depends(require_admin)])
async def list_receipts(db: AsyncSession = Depends(get_db)):
    """List all parsed email receipts (admin only)."""
    result = await db.execute(
        select(EmailReceipt)
        .options(joinedload(EmailReceipt.child).joinedload(ChildProfile.user))
        .order_by(EmailReceipt.payment_date.desc())
    )
    receipts = result.scalars().unique().all()
    output = []
    for r in receipts:
        student_name = None
        if r.child and r.child.user:
            u = r.child.user
            student_name = f"{u.last_name} {u.first_name}".strip()
        output.append(EmailReceiptOut(
            id=r.id,
            receipt_number=r.receipt_number,
            payer_name=r.payer_name,
            amount=r.amount,
            payment_date=r.payment_date,
            child_id=r.child_id,
            student_name=student_name,
            created_at=r.created_at,
        ))
    return output


@router.post("/receipts/parse-emails", dependencies=[Depends(require_admin)])
async def trigger_email_parsing(db: AsyncSession = Depends(get_db)):
    """Manually trigger email inbox parsing for new EasyPay receipts."""
    from app.services.email_parser import run_email_parse
    count = await run_email_parse(db)
    return {"new_receipts": count, "message": f"Обработано новых чеков: {count}"}


# ─── Finance Report ────────────────────────────────────────────────────────────

@router.post("/receipts/rematch", dependencies=[Depends(require_admin)])
async def rematch_receipts(db: AsyncSession = Depends(get_db)):
    from app.services.email_parser import rematch_unlinked_receipts
    count = await rematch_unlinked_receipts(db)
    return {"matched_receipts": count}


@router.get("/finance-report", response_model=List[StudentFinanceRow], dependencies=[Depends(require_admin)])
async def finance_report(
        week_start: Optional[date] = Query(None),
        db: AsyncSession = Depends(get_db),
):
    """Weekly finance report per student."""
    if week_start is None:
        today = date.today()
        week_start = today - timedelta(days=today.weekday())

    week_end = week_start + timedelta(days=6)

    # Completed lessons in the week
    lessons_res = await db.execute(
        select(Lesson.child_id, func.count(Lesson.id).label("cnt"))
        .where(
            Lesson.status == LessonStatus.completed,
            Lesson.date >= week_start,
            Lesson.date <= week_end,
        )
        .group_by(Lesson.child_id)
    )
    lessons_by_child = {row.child_id: row.cnt for row in lessons_res}

    # Paid receipts in the week (cast datetime to date for correct comparison)
    receipts_res = await db.execute(
        select(EmailReceipt.child_id, func.sum(EmailReceipt.amount).label("total"))
        .where(
            EmailReceipt.child_id.isnot(None),
            cast(EmailReceipt.payment_date, Date) >= week_start,
            cast(EmailReceipt.payment_date, Date) <= week_end,
        )
        .group_by(EmailReceipt.child_id)
    )
    amounts_by_child = {row.child_id: row.total for row in receipts_res}

    all_child_ids = set(lessons_by_child) | set(amounts_by_child)

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


@router.patch("/students/{user_id}", dependencies=[Depends(require_admin)])
async def update_admin_student(
    user_id: int,
    payload: AdminStudentUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User)
        .options(joinedload(User.child_profile))
        .where(User.id == user_id, User.role == RoleEnum.child)
    )
    student = result.scalars().unique().one_or_none()
    if not student or not student.child_profile:
        raise HTTPException(status_code=404, detail="Student not found")

    if payload.lesson_price is not None:
        if payload.lesson_price <= 0:
            raise HTTPException(status_code=400, detail="Lesson price must be greater than zero")
        student.child_profile.lesson_price = payload.lesson_price

    await db.commit()
    return {"ok": True, "lesson_price": student.child_profile.lesson_price}


# ─── Ручная привязка Родитель ↔ Ребёнок ────────────────────────────────────────

@router.post("/parent-child/bind", dependencies=[Depends(require_admin)])
async def bind_parent_to_child(payload: BaseParentChildLink, db: AsyncSession = Depends(get_db)):
    """Вручную связать существующего родителя и ребёнка по ID их профилей (Admin only)."""

    # 1. Проверяем, существует ли родитель
    parent_res = await db.execute(select(ParentProfile).where(ParentProfile.id == payload.parent_id))
    parent = parent_res.scalar_one_or_none()
    if not parent:
        raise HTTPException(status_code=404, detail=f"Профиль родителя с ID {payload.parent_id} не найден")

    # 2. Проверяем, существует ли ребёнок
    child_res = await db.execute(select(ChildProfile).where(ChildProfile.id == payload.child_id))
    child = child_res.scalar_one_or_none()
    if not child:
        raise HTTPException(status_code=404, detail=f"Профиль ребёнка с ID {payload.child_id} не найден")

    # 3. Проверяем дубликаты связей
    exist_res = await db.execute(
        select(ParentChild).where(
            ParentChild.parent_id == payload.parent_id,
            ParentChild.child_id == payload.child_id
        )
    )
    if exist_res.scalar_one_or_none():
        return {"message": "Эта связь уже существует в базе данных"}

    # 4. Создаем запись
    new_relation = ParentChild(
        parent_id=payload.parent_id,
        child_id=payload.child_id
    )
    db.add(new_relation)
    await db.commit()

    return {
        "status": "success",
        "message": f"Родитель (ID {payload.parent_id}) успешно связан с ребёнком (ID {payload.child_id})"
    }


# ─── Tutors (admin management) ─────────────────────────────────────────────────

def _serialize_tutor(tutor: TutorProfile) -> TutorProfileOut:
    """TutorProfile.subjects is a list of TutorSubject link rows, not Subject —
    unwrap them into the actual Subject objects expected by TutorProfileOut."""
    return TutorProfileOut(
        id=tutor.id,
        bio=tutor.bio,
        education=tutor.education,
        experience_years=tutor.experience_years,
        rate_per_hour=tutor.rate_per_hour,
        is_published=tutor.is_published,
        user=tutor.user,
        subjects=[ts.subject for ts in tutor.subjects if ts.subject is not None],
    )


@router.get("/tutors", response_model=List[TutorProfileOut], dependencies=[Depends(require_admin)])
async def list_admin_tutors(db: AsyncSession = Depends(get_db)):
    """Список всех репетиторов для админ-панели (вкладка «Репетиторы»)."""
    result = await db.execute(
        select(TutorProfile)
        .options(
            joinedload(TutorProfile.user),
            joinedload(TutorProfile.subjects).joinedload(TutorSubject.subject),
        )
        .order_by(TutorProfile.id)
    )
    tutors = result.unique().scalars().all()
    return [_serialize_tutor(t) for t in tutors]


@router.patch("/tutors/{tutor_id}", response_model=TutorProfileOut, dependencies=[Depends(require_admin)])
async def update_admin_tutor(
    tutor_id: int,
    payload: AdminTutorUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(TutorProfile)
        .options(
            joinedload(TutorProfile.user),
            joinedload(TutorProfile.subjects).joinedload(TutorSubject.subject),
        )
        .where(TutorProfile.id == tutor_id)
    )
    tutor = result.unique().scalar_one_or_none()
    if not tutor:
        raise HTTPException(status_code=404, detail=f"Репетитор с ID {tutor_id} не найден")

    data = payload.model_dump(exclude_unset=True)

    # Профильные поля репетитора
    for field in ("bio", "education", "experience_years", "rate_per_hour", "is_published"):
        if field in data and data[field] is not None:
            setattr(tutor, field, data[field])

    # Поля связанного пользователя (имя, фамилия, аватар)
    user_data = data.get("user")
    if user_data:
        for field in ("first_name", "last_name", "avatar_url"):
            if user_data.get(field) is not None:
                setattr(tutor.user, field, user_data[field])

    # Предметы репетитора — полностью пересобираем список связей
    subject_ids = data.get("subject_ids")
    if subject_ids is not None:
        existing = await db.execute(
            select(TutorSubject).where(TutorSubject.tutor_id == tutor.id)
        )
        for link in existing.scalars().all():
            await db.delete(link)
        await db.flush()

        if subject_ids:
            valid_subjects = await db.execute(
                select(Subject.id).where(Subject.id.in_(subject_ids))
            )
            valid_ids = {row[0] for row in valid_subjects.all()}
            for sid in subject_ids:
                if sid in valid_ids:
                    db.add(TutorSubject(tutor_id=tutor.id, subject_id=sid))

    await db.commit()

    # Перечитываем с подгруженными связями для корректного ответа
    result = await db.execute(
        select(TutorProfile)
        .options(
            joinedload(TutorProfile.user),
            joinedload(TutorProfile.subjects).joinedload(TutorSubject.subject),
        )
        .where(TutorProfile.id == tutor_id)
    )
    return _serialize_tutor(result.unique().scalar_one())


@router.delete("/tutors/{tutor_id}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_admin_tutor(tutor_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TutorProfile).where(TutorProfile.id == tutor_id))
    tutor = result.scalar_one_or_none()
    user_id = tutor.user_id if tutor else None
    if not tutor:
        raise HTTPException(status_code=404, detail=f"Репетитор с ID {tutor_id} не найден")

    lessons_res = await db.execute(select(Lesson.id).where(Lesson.tutor_id == tutor_id).limit(1))
    if lessons_res.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Репетиторов с историей занятий удалить нельзя")

    # Удаляем зависимые записи, чтобы не упереться в внешние ключи
    subj_links = await db.execute(select(TutorSubject).where(TutorSubject.tutor_id == tutor_id))
    for link in subj_links.scalars().all():
        await db.delete(link)

    docs = await db.execute(select(TutorDocument).where(TutorDocument.tutor_id == tutor_id))
    for doc in docs.scalars().all():
        await db.delete(doc)

    for model, field in (
        (TutorContract, TutorContract.tutor_id),
        (Report, Report.tutor_id),
        (Comment, Comment.tutor_id),
        (Act, Act.tutor_id),
    ):
        rows = await db.execute(select(model).where(field == tutor_id))
        for row in rows.scalars().all():
            await db.delete(row)

    if user_id:
        await _delete_invite_codes_for_user(db, user_id)
        notifications = await db.execute(select(Notification).where(Notification.user_id == user_id))
        for notification in notifications.scalars().all():
            await db.delete(notification)

    user = await db.get(User, user_id) if user_id else None
    await db.delete(tutor)
    if user:
        await db.delete(user)
    await db.commit()
    return None


@router.delete("/students/{user_id}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_admin_student(user_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id, User.role == RoleEnum.child))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Student not found")

    result = await db.execute(select(ChildProfile).where(ChildProfile.user_id == user_id))
    child = result.scalar_one_or_none()
    if not child:
        await _delete_invite_codes_for_user(db, user_id)
        notifications = await db.execute(select(Notification).where(Notification.user_id == user_id))
        for notification in notifications.scalars().all():
            await db.delete(notification)
        await db.delete(user)
        await db.commit()
        return None

    child_id = child.id

    parent_links = await db.execute(select(ParentChild).where(ParentChild.child_id == child_id))
    for link in parent_links.scalars().all():
        await db.delete(link)

    lessons = await db.execute(select(Lesson).where(Lesson.child_id == child_id))
    for lesson in lessons.scalars().all():
        hws = await db.execute(select(Homework).where(Homework.lesson_id == lesson.id))
        for hw in hws.scalars().all():
            await db.delete(hw)
        await db.delete(lesson)

    for model, field in (
        (Report, Report.child_id),
        (Material, Material.child_id),
        (Homework, Homework.child_id),
        (ParentContract, ParentContract.child_id),
        (Payment, Payment.child_id),
        (Comment, Comment.child_id),
        (TestResult, TestResult.child_id),
    ):
        rows = await db.execute(select(model).where(field == child_id))
        for row in rows.scalars().all():
            await db.delete(row)

    receipts = await db.execute(select(EmailReceipt).where(EmailReceipt.child_id == child_id))
    for receipt in receipts.scalars().all():
        receipt.child_id = None

    await _delete_invite_codes_for_user(db, user_id)

    notifications = await db.execute(select(Notification).where(Notification.user_id == user_id))
    for notification in notifications.scalars().all():
        await db.delete(notification)

    await db.delete(child)
    await db.delete(user)
    await db.commit()
    return None


# ─── Tutor documents (admin → tutor PDF exchange) ──────────────────────────────

@router.post(
    "/tutor-documents",
    response_model=TutorDocumentOut,
    status_code=201,
    dependencies=[Depends(require_admin)],
)
async def send_tutor_document(payload: TutorDocumentCreate, db: AsyncSession = Depends(get_db)):
    """Админ отправляет документ (например, PDF) выбранному репетитору."""
    tutor_res = await db.execute(select(TutorProfile).where(TutorProfile.id == payload.tutor_id))
    if not tutor_res.scalar_one_or_none():
        raise HTTPException(status_code=404, detail=f"Репетитор с ID {payload.tutor_id} не найден")

    doc = TutorDocument(tutor_id=payload.tutor_id, title=payload.title, file_url=payload.file_url)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


@router.get(
    "/tutor-documents",
    response_model=List[TutorDocumentOut],
    dependencies=[Depends(require_admin)],
)
async def list_tutor_documents(
    tutor_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Список документов, отправленных репетиторам (можно отфильтровать по репетитору)."""
    q = select(TutorDocument)
    if tutor_id:
        q = q.where(TutorDocument.tutor_id == tutor_id)
    result = await db.execute(q.order_by(TutorDocument.created_at.desc()))
    return result.scalars().all()


# ─── Acts (просмотр подписанных актов от репетиторов) ──────────────────────────

@router.get("/acts", response_model=List[ActOut], dependencies=[Depends(require_admin)])
async def list_admin_acts(
    tutor_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Список актов (в т.ч. подписанных репетитором), можно отфильтровать по репетитору."""
    q = select(Act)
    if tutor_id:
        q = q.where(Act.tutor_id == tutor_id)
    result = await db.execute(q.order_by(Act.created_at.desc()))
    return result.scalars().all()
