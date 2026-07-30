from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_password_hash,
    verify_password,
)
from app.db.session import get_db
from app.models.models import (
    ChildProfile,
    InviteCode,
    ParentChild,
    ParentProfile,
    RoleEnum,
    TutorProfile,
    User,
)
from app.schemas.schemas import LoginRequest, Token, TokenRefresh, UserCreate, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=Token)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный email или пароль")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Аккаунт отключён")

    access = create_access_token({"sub": str(user.id), "role": user.role})
    refresh = create_refresh_token({"sub": str(user.id)})
    return Token(access_token=access, refresh_token=refresh)


@router.post("/refresh", response_model=Token)
async def refresh(data: TokenRefresh, db: AsyncSession = Depends(get_db)):
    payload = decode_token(data.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Недействительный refresh token")

    result = await db.execute(select(User).where(User.id == int(payload["sub"])))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Пользователь не найден")

    access = create_access_token({"sub": str(user.id), "role": user.role})
    refresh_new = create_refresh_token({"sub": str(user.id)})
    return Token(access_token=access, refresh_token=refresh_new)


@router.get("/invite-codes/validate")
async def validate_invite_code(code: str, db: AsyncSession = Depends(get_db)):
    invite = await db.scalar(select(InviteCode).where(InviteCode.code == code))
    if not invite or invite.is_used:
        raise HTTPException(status_code=404, detail="Код не найден или уже использован")
    return {
        "code": invite.code,
        "role": invite.role,
        "description": invite.description,
        "is_used": invite.is_used,
    }


async def _ensure_profile(db: AsyncSession, user: User) -> None:
    if user.role == RoleEnum.tutor:
        exists = await db.scalar(select(TutorProfile).where(TutorProfile.user_id == user.id))
        if not exists:
            db.add(TutorProfile(user_id=user.id))
    elif user.role == RoleEnum.child:
        exists = await db.scalar(select(ChildProfile).where(ChildProfile.user_id == user.id))
        if not exists:
            db.add(ChildProfile(user_id=user.id))
    elif user.role == RoleEnum.parent:
        exists = await db.scalar(select(ParentProfile).where(ParentProfile.user_id == user.id))
        if not exists:
            db.add(ParentProfile(user_id=user.id))


async def _link_parent_child_for_invite(db: AsyncSession, invite: InviteCode, user: User) -> None:
    pair_res = await db.execute(
        select(InviteCode).where(
            (InviteCode.id == invite.linked_code_id) | (InviteCode.linked_code_id == invite.id)
        )
    )
    pair_invites = pair_res.scalars().all()
    partner = next((pi for pi in pair_invites if pi.id != invite.id and pi.used_by_user_id), None)
    if not partner:
        return

    if user.role == RoleEnum.parent:
        parent_user_id = user.id
        child_user_id = partner.used_by_user_id
    else:
        parent_user_id = partner.used_by_user_id
        child_user_id = user.id

    parent_profile = await db.scalar(select(ParentProfile).where(ParentProfile.user_id == parent_user_id))
    child_profile = await db.scalar(select(ChildProfile).where(ChildProfile.user_id == child_user_id))
    if not parent_profile or not child_profile:
        return

    exists = await db.scalar(
        select(ParentChild).where(
            ParentChild.parent_id == parent_profile.id,
            ParentChild.child_id == child_profile.id,
        )
    )
    if not exists:
        db.add(ParentChild(parent_id=parent_profile.id, child_id=child_profile.id))


@router.post("/register", response_model=UserOut)
async def register(data: UserCreate, db: AsyncSession = Depends(get_db)):
    invite = await db.scalar(select(InviteCode).where(InviteCode.code == data.invite_code))
    if not invite or invite.is_used:
        raise HTTPException(status_code=400, detail="Код не найден или уже использован")
    if invite.role != data.role:
        raise HTTPException(status_code=400, detail="Код доступа не подходит для выбранной роли")

    existing_email = await db.scalar(select(User).where(User.email == data.email))
    target_user: User | None = None
    if invite.used_by_user_id:
        target_user = await db.scalar(select(User).where(User.id == invite.used_by_user_id))

    if existing_email and (not target_user or existing_email.id != target_user.id):
        raise HTTPException(status_code=400, detail="Пользователь с таким email уже зарегистрирован")

    if target_user:
        target_user.email = data.email
        target_user.hashed_password = get_password_hash(data.password)
        target_user.first_name = data.first_name
        target_user.last_name = data.last_name
        target_user.middle_name = data.middle_name or ""
        target_user.phone = data.phone or target_user.phone
        target_user.role = data.role
        target_user.is_active = True
        new_user = target_user
    else:
        new_user = User(
            email=data.email,
            hashed_password=get_password_hash(data.password),
            first_name=data.first_name,
            last_name=data.last_name,
            middle_name=data.middle_name or "",
            phone=data.phone or "+375000000000",
            role=data.role,
            is_active=True,
        )
        db.add(new_user)
        await db.flush()

    await _ensure_profile(db, new_user)
    await db.flush()

    invite.is_used = True
    invite.used_by_user_id = new_user.id
    await _link_parent_child_for_invite(db, invite, new_user)

    await db.commit()
    await db.refresh(new_user)
    return new_user
