from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.models import (
    ChildProfile,
    Lesson,
    LessonStatus,
    Notification,
    ParentChild,
    ParentProfile,
    Report,
    RoleEnum,
    Subject,
    TutorProfile,
    User,
)
from app.schemas.schemas import LessonCreate, LessonUpdate

router = APIRouter(prefix="/lessons", tags=["lessons"])


def _user_name(user: User | None) -> str:
    if not user:
        return ""
    return f"{user.last_name or ''} {user.first_name or ''}".strip() or user.email


def _lesson_to_dict(l: Lesson) -> dict:
    return {
        "id": l.id,
        "tutor_id": l.tutor_id,
        "child_id": l.child_id,
        "subject_id": l.subject_id,
        "student_name": _user_name(l.child.user if l.child else None),
        "tutor_name": _user_name(l.tutor.user if l.tutor else None),
        "subject_name": l.subject.name if l.subject else None,
        "date": str(l.date),
        "time_start": str(l.time_start),
        "time_end": str(l.time_end),
        "status": l.status,
        "cancel_reason": l.cancel_reason,
        "notes": l.notes,
        "is_free_trial": l.is_free_trial,
        "created_at": str(l.created_at),
    }


def _lesson_options(stmt):
    return stmt.options(
        joinedload(Lesson.tutor).joinedload(TutorProfile.user),
        joinedload(Lesson.child).joinedload(ChildProfile.user),
        joinedload(Lesson.subject),
    )


async def _notify(db: AsyncSession, user_id: int | None, title: str, body: str) -> None:
    if user_id:
        db.add(Notification(user_id=user_id, title=title, body=body))


async def _notify_admins(db: AsyncSession, title: str, body: str) -> None:
    result = await db.execute(select(User.id).where(User.role == RoleEnum.admin, User.is_active == True))
    for user_id in result.scalars().all():
        db.add(Notification(user_id=user_id, title=title, body=body))


async def _load_lesson(db: AsyncSession, lesson_id: int) -> Lesson | None:
    result = await db.execute(_lesson_options(select(Lesson).where(Lesson.id == lesson_id)))
    return result.scalars().unique().one_or_none()


def _can_access_lesson(user: User, lesson: Lesson) -> bool:
    if user.role == RoleEnum.admin:
        return True
    if user.role == RoleEnum.tutor and user.tutor_profile:
        return lesson.tutor_id == user.tutor_profile.id
    if user.role == RoleEnum.child and user.child_profile:
        return lesson.child_id == user.child_profile.id
    if user.role == RoleEnum.parent and user.parent_profile:
        return any(pc.child_id == lesson.child_id for pc in user.parent_profile.children)
    return False


async def _require_report_for_fifth_lesson(db: AsyncSession, lesson: Lesson) -> None:
    completed_before = await db.scalar(
        select(func.count(Lesson.id)).where(
            Lesson.child_id == lesson.child_id,
            Lesson.status == LessonStatus.completed,
            Lesson.id != lesson.id,
        )
    ) or 0
    next_number = completed_before + 1
    if next_number % 5 != 0:
        return

    report_exists = await db.scalar(
        select(func.count(Report.id)).where(
            Report.child_id == lesson.child_id,
            Report.lesson_id == lesson.id,
        )
    ) or 0
    if not report_exists:
        raise HTTPException(
            status_code=400,
            detail="Для каждого 5-го занятия ученика нужно сначала заполнить отчёт.",
        )


@router.get("/", response_model=List[dict])
async def get_lessons(
    tutor_id: Optional[int] = Query(None),
    child_id: Optional[int] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    status: Optional[LessonStatus] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    filters = []

    if current_user.role == RoleEnum.tutor:
        if not current_user.tutor_profile:
            return []
        filters.append(Lesson.tutor_id == current_user.tutor_profile.id)
    elif current_user.role == RoleEnum.child:
        if not current_user.child_profile:
            return []
        filters.append(Lesson.child_id == current_user.child_profile.id)
    elif current_user.role == RoleEnum.parent:
        if not current_user.parent_profile:
            return []
        child_ids = [pc.child_id for pc in current_user.parent_profile.children]
        if not child_ids:
            return []
        filters.append(Lesson.child_id.in_(child_ids))
    elif current_user.role == RoleEnum.admin:
        if tutor_id:
            filters.append(Lesson.tutor_id == tutor_id)
        if child_id:
            filters.append(Lesson.child_id == child_id)
    else:
        raise HTTPException(status_code=403, detail="Недостаточно прав")

    if date_from:
        filters.append(Lesson.date >= date_from)
    if date_to:
        filters.append(Lesson.date <= date_to)
    if status:
        filters.append(Lesson.status == status)

    stmt = select(Lesson)
    if filters:
        stmt = stmt.where(and_(*filters))
    stmt = _lesson_options(stmt.order_by(Lesson.date, Lesson.time_start))
    result = await db.execute(stmt)
    return [_lesson_to_dict(l) for l in result.scalars().unique().all()]


@router.post("/", response_model=dict, status_code=201)
async def create_lesson(
    data: LessonCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tutor = await db.scalar(select(TutorProfile).where(TutorProfile.id == data.tutor_id).options(joinedload(TutorProfile.user)))
    if not tutor:
        raise HTTPException(status_code=422, detail=f"Репетитор с профилем id={data.tutor_id} не найден")

    child = await db.scalar(select(ChildProfile).where(ChildProfile.id == data.child_id).options(joinedload(ChildProfile.user)))
    if not child:
        raise HTTPException(status_code=422, detail=f"Ученик с профилем id={data.child_id} не найден")

    if current_user.role == RoleEnum.tutor and current_user.tutor_profile:
        if current_user.tutor_profile.id != data.tutor_id:
            raise HTTPException(status_code=403, detail="Репетитор может создавать занятия только себе")
    elif current_user.role != RoleEnum.admin:
        raise HTTPException(status_code=403, detail="Создавать занятия может только админ или репетитор")

    lesson = Lesson(**data.model_dump())
    db.add(lesson)
    await db.flush()

    student_name = _user_name(child.user)
    if current_user.role == RoleEnum.admin:
        await _notify(
            db,
            tutor.user_id,
            "Новое занятие",
            f"Администратор назначил занятие с учеником {student_name} на {lesson.date} {lesson.time_start}.",
        )
    else:
        await _notify_admins(
            db,
            "Новое занятие от репетитора",
            f"{_user_name(current_user)} назначил занятие с учеником {student_name} на {lesson.date} {lesson.time_start}.",
        )

    await db.commit()
    fresh = await _load_lesson(db, lesson.id)
    return _lesson_to_dict(fresh)


@router.get("/tutor/my-students", response_model=list[dict])
async def get_tutor_students(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in (RoleEnum.tutor, RoleEnum.admin):
        raise HTTPException(status_code=403, detail="Недостаточно прав")

    tutor_profile_id = current_user.tutor_profile.id if current_user.role == RoleEnum.tutor and current_user.tutor_profile else None
    stmt = select(Lesson.child_id)
    if tutor_profile_id:
        stmt = stmt.where(Lesson.tutor_id == tutor_profile_id)
    result = await db.execute(stmt)
    child_ids = list({item for item in result.scalars().all()})
    if not child_ids:
        return []

    child_result = await db.execute(
        select(ChildProfile)
        .options(
            joinedload(ChildProfile.user),
            selectinload(ChildProfile.parents).selectinload(ParentChild.parent).joinedload(ParentProfile.user),
        )
        .where(ChildProfile.id.in_(child_ids))
    )
    children = child_result.scalars().unique().all()

    output = []
    for child in children:
        parent_user = None
        if child.parents:
            parent_user = child.parents[0].parent.user
        output.append({
            "child_profile_id": child.id,
            "user_id": child.user.id,
            "first_name": child.user.first_name,
            "last_name": child.user.last_name,
            "email": child.user.email,
            "student_phone": child.user.phone,
            "parent_name": _user_name(parent_user),
            "parent_phone": parent_user.phone if parent_user else None,
            "lessons_per_week": child.lessons_per_week,
            "notes": child.notes,
            "subjects": [s.strip() for s in (child.subjects_text or "").split(",") if s.strip()],
        })
    return output


@router.get("/students-list/all", response_model=list[dict])
async def list_students_for_admin(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != RoleEnum.admin:
        raise HTTPException(status_code=403, detail="Список всех учеников доступен только админу")

    result = await db.execute(
        select(User)
        .where(User.role == RoleEnum.child, User.is_active == True)
        .options(joinedload(User.child_profile))
        .order_by(User.last_name, User.first_name)
    )
    output = []
    for user in result.scalars().unique().all():
        if not user.child_profile:
            continue
        output.append({
            "id": user.id,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": user.email,
            "child_profile": {"id": user.child_profile.id},
        })
    return output


@router.get("/{lesson_id}", response_model=dict)
async def get_lesson(
    lesson_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    lesson = await _load_lesson(db, lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Занятие не найдено")
    if not _can_access_lesson(current_user, lesson):
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return _lesson_to_dict(lesson)


@router.patch("/{lesson_id}", response_model=dict)
async def update_lesson(
    lesson_id: int,
    data: LessonUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    lesson = await _load_lesson(db, lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Занятие не найдено")
    if current_user.role not in (RoleEnum.admin, RoleEnum.tutor):
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    if current_user.role == RoleEnum.tutor and (not current_user.tutor_profile or lesson.tutor_id != current_user.tutor_profile.id):
        raise HTTPException(status_code=403, detail="Недостаточно прав")

    updates = data.model_dump(exclude_none=True)
    if current_user.role != RoleEnum.admin:
        for protected_field in ("tutor_id", "child_id", "subject_id"):
            if protected_field in updates:
                raise HTTPException(status_code=403, detail="Only admin can change lesson participants and subject")

    if "tutor_id" in updates and not await db.scalar(select(TutorProfile.id).where(TutorProfile.id == updates["tutor_id"])):
        raise HTTPException(status_code=422, detail=f"Tutor profile id={updates['tutor_id']} not found")
    if "child_id" in updates and not await db.scalar(select(ChildProfile.id).where(ChildProfile.id == updates["child_id"])):
        raise HTTPException(status_code=422, detail=f"Child profile id={updates['child_id']} not found")
    if "subject_id" in updates and not await db.scalar(select(Subject.id).where(Subject.id == updates["subject_id"])):
        raise HTTPException(status_code=422, detail=f"Subject id={updates['subject_id']} not found")

    if updates.get("status") == LessonStatus.completed and lesson.status != LessonStatus.completed:
        await _require_report_for_fifth_lesson(db, lesson)

    old_status = lesson.status
    for field, value in updates.items():
        setattr(lesson, field, value)

    target_title = "Занятие изменено"
    student_name = _user_name(lesson.child.user if lesson.child else None)
    if current_user.role == RoleEnum.admin:
        await _notify(
            db,
            lesson.tutor.user_id if lesson.tutor else None,
            target_title,
            f"Администратор изменил занятие с учеником {student_name} на {lesson.date}.",
        )
    else:
        await _notify_admins(
            db,
            target_title,
            f"{_user_name(current_user)} изменил занятие с учеником {student_name} на {lesson.date}.",
        )

    if "status" in updates and updates["status"] != old_status:
        await _notify_admins(
            db,
            "Статус занятия обновлён",
            f"Занятие ученика {student_name} получило статус {updates['status']}.",
        )

    await db.commit()
    fresh = await _load_lesson(db, lesson_id)
    return _lesson_to_dict(fresh)


@router.delete("/{lesson_id}", status_code=204)
async def delete_lesson(
    lesson_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    lesson = await _load_lesson(db, lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Занятие не найдено")
    if current_user.role != RoleEnum.admin and (
        current_user.role != RoleEnum.tutor
        or not current_user.tutor_profile
        or lesson.tutor_id != current_user.tutor_profile.id
    ):
        raise HTTPException(status_code=403, detail="Недостаточно прав")

    student_name = _user_name(lesson.child.user if lesson.child else None)
    if current_user.role == RoleEnum.admin:
        await _notify(db, lesson.tutor.user_id if lesson.tutor else None, "Занятие удалено", f"Администратор удалил занятие с учеником {student_name}.")
    else:
        await _notify_admins(db, "Занятие удалено", f"{_user_name(current_user)} удалил занятие с учеником {student_name}.")

    await db.delete(lesson)
    await db.commit()
