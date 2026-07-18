from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.models import User, TutorProfile, RoleEnum
from app.schemas.schemas import UserOut, UserOutWithProfiles, UserUpdate, TutorProfileOut, TutorProfileUpdate, UserMeOut
from app.core.deps import get_current_user, require_admin

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserMeOut)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=UserOut)
async def update_me(
    data: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(current_user, field, value)
    await db.commit()
    await db.refresh(current_user)
    return current_user


@router.get("/", response_model=List[UserOutWithProfiles])
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(
        select(User).options(
            selectinload(User.tutor_profile),
            selectinload(User.child_profile),
            selectinload(User.parent_profile),
        )
    )
    return result.scalars().all()


@router.get("/tutors/public", response_model=list[dict])
async def list_published_tutors_compat(db: AsyncSession = Depends(get_db)):
    users_res = await db.execute(
        select(User)
        .where(User.role == RoleEnum.tutor)
        .options(selectinload(User.tutor_profile))
    )
    tutor_users = users_res.scalars().unique().all()

    output = []
    created = False
    for u in tutor_users:
        profile = u.tutor_profile
        if profile is None:
            profile = TutorProfile(user_id=u.id)
            db.add(profile)
            await db.flush()
            created = True

        output.append({
            "id": profile.id,
            "user": {
                "id": u.id,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "email": u.email,
                "avatar_url": u.avatar_url,
            },
            "bio": profile.bio,
            "education": profile.education,
            "experience_years": profile.experience_years,
            "rate_per_hour": profile.rate_per_hour,
            "is_published": profile.is_published,
            "subjects": [],
        })

    if created:
        await db.commit()
    return output


@router.get("/students/list", response_model=list[dict])
async def list_students_for_tutor_compat(db: AsyncSession = Depends(get_db)):
    query = select(User).where(User.role == RoleEnum.child).options(selectinload(User.child_profile))
    result = await db.execute(query)
    students = result.scalars().all()

    output = []
    for s in students:
        output.append({
            "id": s.id,
            "first_name": s.first_name,
            "last_name": s.last_name,
            "email": s.email,
            "child_profile": {"id": s.child_profile.id if s.child_profile else s.id},
        })
    return output


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != RoleEnum.admin and current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


# ─── Tutor profiles (public list for website) ─────────────────────────────────

@router.get("/tutors/public", response_model=list[dict])
async def list_published_tutors(db: AsyncSession = Depends(get_db)):
    """All tutors (with auto-create of missing profiles) — used in admin dropdowns."""
    from sqlalchemy.orm import joinedload as _jl

    # Load all users with role=tutor
    users_res = await db.execute(
        select(User)
        .where(User.role == RoleEnum.tutor)
        .options(_jl(User.tutor_profile))
    )
    tutor_users = users_res.scalars().unique().all()

    output = []
    for u in tutor_users:
        profile = u.tutor_profile
        if profile is None:
            # Auto-create missing TutorProfile
            profile = TutorProfile(user_id=u.id)
            db.add(profile)
            await db.flush()

        output.append({
            "id": profile.id,           # tutor_profile.id  — used as tutor_id in lessons
            "user": {
                "id": u.id,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "email": u.email,
            },
            "rate_per_hour": profile.rate_per_hour,
            "is_published": profile.is_published,
        })

    if any(u.tutor_profile is None for u in tutor_users):
        await db.commit()

    return output


@router.patch("/tutors/me", response_model=TutorProfileOut)
async def update_tutor_profile(
    data: TutorProfileUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != RoleEnum.tutor:
        raise HTTPException(status_code=403, detail="Only tutors can update tutor profile")
    profile = current_user.tutor_profile
    if not profile:
        raise HTTPException(status_code=404, detail="Tutor profile not found")
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(profile, field, value)
    await db.commit()
    await db.refresh(profile)
    return profile


@router.get("/students/list", response_model=list[dict])
async def list_students_for_tutor(
        db: AsyncSession = Depends(get_db)
):
    """
    Возвращает список всех учеников школы для Репетиторов и Админов.
    """
    try:
        # 1. Делаем чистый запрос к базе через SQLAlchemy
        query = select(User).where(User.role == "child")
        result = await db.execute(query)
        students = result.scalars().all()

        # 2. Формируем безопасный список для фронтенда
        output = []
        for s in students:
            output.append({
                "id": s.id,
                "first_name": s.first_name,
                "last_name": s.last_name,
                "email": s.email,
                # Если у юзера еще нет child_profile, отдаем его id, чтобы фронт не упал
                "child_profile": {"id": s.id}
            })

        return output
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка базы данных: {str(e)}"
        )
