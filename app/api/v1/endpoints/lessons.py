from typing import List, Optional
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from sqlalchemy.orm import joinedload, selectinload
from app.db.session import get_db
from app.models.models import Lesson, User, RoleEnum, LessonStatus, ChildProfile, TutorProfile
from app.schemas.schemas import LessonCreate, LessonUpdate, LessonOut
from app.core.deps import get_current_user

router = APIRouter(prefix="/lessons", tags=["lessons"])


def _lesson_to_dict(l: Lesson) -> dict:
    """Convert ORM Lesson to a dict with populated student/tutor/subject names."""
    student_name = None
    tutor_name = None
    subject_name = None

    if l.child and l.child.user:
        u = l.child.user
        student_name = f"{u.last_name} {u.first_name}".strip() or u.email
    if l.tutor and l.tutor.user:
        u = l.tutor.user
        tutor_name = f"{u.last_name} {u.first_name}".strip() or u.email
    if l.subject:
        subject_name = l.subject.name

    return {
        "id": l.id,
        "tutor_id": l.tutor_id,
        "child_id": l.child_id,
        "subject_id": l.subject_id,
        "student_name": student_name,
        "tutor_name": tutor_name,
        "subject_name": subject_name,
        "date": str(l.date),
        "time_start": str(l.time_start),
        "time_end": str(l.time_end),
        "status": l.status,
        "cancel_reason": l.cancel_reason,
        "notes": l.notes,
        "is_free_trial": l.is_free_trial,
        "created_at": str(l.created_at),
    }


def _lessons_query_with_joins(base_q):
    """Apply eager-load joins to a Lesson select query."""
    return base_q.options(
        joinedload(Lesson.tutor).joinedload(TutorProfile.user),
        joinedload(Lesson.child).joinedload(ChildProfile.user),
        joinedload(Lesson.subject),
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
        if current_user.tutor_profile:
            filters.append(Lesson.tutor_id == current_user.tutor_profile.id)
        else:
            return []
    elif current_user.role == RoleEnum.child:
        if current_user.child_profile:
            filters.append(Lesson.child_id == current_user.child_profile.id)
        else:
            return []
    elif current_user.role == RoleEnum.parent:
        if current_user.parent_profile:
            child_ids = [pc.child_id for pc in current_user.parent_profile.children]
            if child_ids:
                filters.append(Lesson.child_id.in_(child_ids))
            elif child_id:
                filters.append(Lesson.child_id == child_id)
            else:
                return []
        elif child_id:
            filters.append(Lesson.child_id == child_id)
        else:
            return []
    else:
        # admin sees all, can filter
        if tutor_id:
            filters.append(Lesson.tutor_id == tutor_id)
        if child_id:
            filters.append(Lesson.child_id == child_id)

    if date_from:
        filters.append(Lesson.date >= date_from)
    if date_to:
        filters.append(Lesson.date <= date_to)
    if status:
        filters.append(Lesson.status == status)

    q = select(Lesson)
    if filters:
        q = q.where(and_(*filters))
    q = q.order_by(Lesson.date, Lesson.time_start)

    q = _lessons_query_with_joins(q)
    result = await db.execute(q)
    lessons = result.scalars().unique().all()
    return [_lesson_to_dict(l) for l in lessons]


@router.post("/", response_model=dict, status_code=201)
async def create_lesson(
        data: LessonCreate,
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user),
):
    from sqlalchemy.exc import IntegrityError

    # Validate tutor_profile exists
    tp_res = await db.execute(select(TutorProfile).where(TutorProfile.id == data.tutor_id))
    if not tp_res.scalar_one_or_none():
        raise HTTPException(status_code=422, detail=f"Репетитор с профилем id={data.tutor_id} не найден")

    # Validate child_profile exists
    cp_res = await db.execute(select(ChildProfile).where(ChildProfile.id == data.child_id))
    if not cp_res.scalar_one_or_none():
        raise HTTPException(status_code=422,
                            detail=f"Ученик с профилем id={data.child_id} не найден. Попросите ученика войти в систему хотя бы раз.")

    try:
        lesson = Lesson(**data.model_dump())
        db.add(lesson)
        await db.commit()

        # Запрашиваем созданный урок из базы сразу со всеми связями
        stmt = (
            select(Lesson)
            .where(Lesson.id == lesson.id)
            .options(
                selectinload(Lesson.child).selectinload(ChildProfile.user),
                selectinload(Lesson.tutor).selectinload(TutorProfile.user),
                selectinload(Lesson.subject)
            )
        )
        res = await db.execute(stmt)
        fresh_lesson = res.scalar_one()

        return _lesson_to_dict(fresh_lesson)

    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=422, detail=f"Ошибка базы данных: {str(e.orig)}")


@router.get("/tutor/my-students", response_model=list[dict])
async def get_tutor_students(
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user),
):
    """Return unique students who have lessons with the current tutor."""
    if current_user.role not in (RoleEnum.tutor, RoleEnum.admin):
        raise HTTPException(status_code=403, detail="Only tutors can access this")

    tutor_profile_id = None
    if current_user.role == RoleEnum.tutor:
        if not current_user.tutor_profile:
            return []
        tutor_profile_id = current_user.tutor_profile.id

    q = select(Lesson)
    if tutor_profile_id:
        q = q.where(Lesson.tutor_id == tutor_profile_id)

    result = await db.execute(q)
    lessons = result.scalars().all()

    child_ids = list({l.child_id for l in lessons})
    if not child_ids:
        return []

    child_result = await db.execute(
        select(ChildProfile)
        .options(joinedload(ChildProfile.user))
        .where(ChildProfile.id.in_(child_ids))
    )
    children = child_result.scalars().unique().all()

    output = []
    for c in children:
        if c.user:
            output.append({
                "child_profile_id": c.id,
                "user_id": c.user.id,
                "first_name": c.user.first_name,
                "last_name": c.user.last_name,
                "email": c.user.email,
            })

    return output


@router.get("/students-list/all", response_model=list[dict])
async def list_students_for_tutor_compat(db: AsyncSession = Depends(get_db)):
    try:
        query = (
            select(User)
            .where(User.role == RoleEnum.child)
            .options(joinedload(User.child_profile))
        )
        result = await db.execute(query)
        students = result.scalars().unique().all()

        output = []
        for s in students:
            cp = s.child_profile
            if cp is None:
                cp = ChildProfile(user_id=s.id)
                db.add(cp)
                await db.flush()

            output.append({
                "id": s.id,
                "first_name": s.first_name,
                "last_name": s.last_name,
                "email": s.email,
                "child_profile": {"id": cp.id},
            })

        if output:
            await db.commit()
        return output
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@router.get("/tutor-calendar/all", response_model=list[dict])
async def get_lessons_for_tutor_compat(db: AsyncSession = Depends(get_db)):
    query = select(Lesson).options(joinedload(Lesson.child).joinedload(ChildProfile.user))
    result = await db.execute(query)
    lessons = result.scalars().unique().all()

    output = []
    for l in lessons:
        first_name = "Student"
        last_name = ""
        email = ""

        if l.child and l.child.user:
            first_name = l.child.user.first_name
            last_name = l.child.user.last_name
            email = l.child.user.email

        output.append({
            "id": l.id,
            "tutor_id": l.tutor_id,
            "child_id": l.child_id,
            "subject_id": l.subject_id,
            "date": str(l.date),
            "time_start": str(l.time_start),
            "time_end": str(l.time_end),
            "status": l.status,
            "notes": l.notes or "",
            "is_free_trial": l.is_free_trial,
            "child": {
                "id": l.child_id,
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
            } if l.child else None,
        })
    return output


@router.get("/{lesson_id}", response_model=LessonOut)
async def get_lesson(
        lesson_id: int,
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user),
):
    q = select(Lesson).where(Lesson.id == lesson_id).options(
        joinedload(Lesson.tutor).joinedload(TutorProfile.user),
        joinedload(Lesson.child).joinedload(ChildProfile.user),
        joinedload(Lesson.subject)
    )

    result = await db.execute(q)
    lesson = result.scalar_one_or_none()

    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")

    if current_user.role == RoleEnum.tutor and current_user.tutor_profile:
        if lesson.tutor_id != current_user.tutor_profile.id:
            raise HTTPException(status_code=403, detail="Not enough permissions")
    if current_user.role == RoleEnum.child and current_user.child_profile:
        if lesson.child_id != current_user.child_profile.id:
            raise HTTPException(status_code=403, detail="Not enough permissions")

    return lesson


@router.patch("/{lesson_id}", response_model=dict)
async def update_lesson(
        lesson_id: int,
        data: LessonUpdate,
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Lesson)
        .where(Lesson.id == lesson_id)
    )
    lesson = result.scalar_one_or_none()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")

    for field, value in data.model_dump(exclude_none=True).items():
        setattr(lesson, field, value)

    await db.commit()

    # --- ИСПРАВЛЕНИЕ ТУТ: Подгружаем связи после обновления, чтобы не было ошибки 500 ---
    stmt = (
        select(Lesson)
        .where(Lesson.id == lesson_id)
        .options(
            selectinload(Lesson.child).selectinload(ChildProfile.user),
            selectinload(Lesson.tutor).selectinload(TutorProfile.user),
            selectinload(Lesson.subject)
        )
    )
    res = await db.execute(stmt)
    fresh_lesson = res.scalar_one()

    return _lesson_to_dict(fresh_lesson)


@router.delete("/{lesson_id}", status_code=204)
async def delete_lesson(
        lesson_id: int,
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Lesson).where(Lesson.id == lesson_id))
    lesson = result.scalar_one_or_none()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    await db.delete(lesson)
    await db.commit()


@router.get("/students-list/all", response_model=list[dict])
async def list_students_for_tutor_fixed(db: AsyncSession = Depends(get_db)):
    try:
        query = (
            select(User)
            .where(User.role == "child")
            .options(joinedload(User.child_profile))
        )
        result = await db.execute(query)
        students = result.scalars().unique().all()

        output = []
        for s in students:
            cp = s.child_profile
            if cp is None:
                from app.models.models import ChildProfile as _CP
                cp = _CP(user_id=s.id)
                db.add(cp)
                await db.flush()

            output.append({
                "id": s.id,
                "first_name": s.first_name,
                "last_name": s.last_name,
                "email": s.email,
                "child_profile": {"id": cp.id}
            })

        if output:
            await db.commit()
        return output
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)}")


@router.get("/tutor-calendar/all", response_model=list[dict])
async def get_lessons_for_tutor_fixed(
        db: AsyncSession = Depends(get_db)
):
    try:
        query = select(Lesson).options(joinedload(Lesson.child).joinedload(ChildProfile.user))
        result = await db.execute(query)
        lessons = result.scalars().unique().all()

        output = []
        for l in lessons:
            first_name = "Ученик"
            last_name = ""
            email = ""

            if l.child:
                if hasattr(l.child, "user") and l.child.user is not None:
                    first_name = getattr(l.child.user, "first_name", "Ученик")
                    last_name = getattr(l.child.user, "last_name", "")
                    email = getattr(l.child.user, "email", "")
                else:
                    first_name = getattr(l.child, "first_name", "Ученик")
                    last_name = getattr(l.child, "last_name", "")
                    email = getattr(l.child, "email", "")

            output.append({
                "id": l.id,
                "tutor_id": l.tutor_id,
                "child_id": l.child_id,
                "subject_id": l.subject_id,
                "date": str(l.date),
                "time_start": str(l.time_start),
                "time_end": str(l.time_end),
                "status": l.status,
                "notes": l.notes or "",
                "is_free_trial": l.is_free_trial,
                "child": {
                    "id": l.child_id,
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": email
                } if l.child else None
            })
        return output
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
