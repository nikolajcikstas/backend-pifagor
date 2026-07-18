from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.session import get_db
from app.models.models import User, RoleEnum, TutorProfile, ChildProfile, ParentProfile, InviteCode
from app.schemas.schemas import Token, LoginRequest, UserCreate, UserOut, TokenRefresh
from app.core.security import (
    verify_password, get_password_hash,
    create_access_token, create_refresh_token, decode_token
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=Token)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")

    access = create_access_token({"sub": str(user.id), "role": user.role})
    refresh = create_refresh_token({"sub": str(user.id)})
    return Token(access_token=access, refresh_token=refresh)


@router.post("/refresh", response_model=Token)
async def refresh(data: TokenRefresh, db: AsyncSession = Depends(get_db)):
    payload = decode_token(data.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    result = await db.execute(select(User).where(User.id == int(payload["sub"])))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    access = create_access_token({"sub": str(user.id), "role": user.role})
    refresh_new = create_refresh_token({"sub": str(user.id)})
    return Token(access_token=access, refresh_token=refresh_new)


@router.post("/register", response_model=UserOut)
async def register(
        data: UserCreate,
        db: AsyncSession = Depends(get_db)
):
    # Проверяем, свободен ли email
    user_query = select(User).where(User.email == data.email)
    user_result = await db.execute(user_query)
    if user_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Пользователь с таким email уже зарегистрирован")

    # Хешируем пароль
    hashed_password = get_password_hash(data.password)

    # Создаем пользователя
    new_user = User(
        email=data.email,
        hashed_password=hashed_password,
        first_name=data.first_name,
        last_name=data.last_name,
        middle_name=data.middle_name or "",
        phone=data.phone or "+375000000000",
        role=data.role
    )

    db.add(new_user)
    await db.flush()  # Получаем new_user.id без полного коммита

    # Создаем профиль в зависимости от роли
    if data.role == RoleEnum.tutor:
        db.add(TutorProfile(user_id=new_user.id))
    elif data.role == RoleEnum.child:
        db.add(ChildProfile(user_id=new_user.id))
    elif data.role == RoleEnum.parent:
        db.add(ParentProfile(user_id=new_user.id))

    # Маркаем инвайт-код как использованный и связываем родителей и детей
    if data.invite_code:
        invite_res = await db.execute(
            select(InviteCode).where(InviteCode.code == data.invite_code)
        )
        invite = invite_res.scalar_one_or_none()

        if invite:
            invite.is_used = True
            invite.used_by_user_id = new_user.id  # Сохраняем, кто активировал код

            # Проверяем, есть ли связанный парный код (для связки Родитель <-> Ребёнок)
            pair_query = select(InviteCode).where(
                (InviteCode.id == invite.linked_code_id) |
                (InviteCode.linked_code_id == invite.id)
            )
            pair_res = await db.execute(pair_query)
            pair_invites = pair_res.scalars().all()

            # Ищем, активирован ли уже второй код из этой пары
            partner_invite = None
            for pi in pair_invites:
                if pi.id != invite.id and pi.is_used and pi.used_by_user_id:
                    partner_invite = pi
                    break

            # Если партнер по коду нашелся (значит, он зарегистрировался ранее)
            if partner_invite:
                current_role = data.role
                partner_user_id = partner_invite.used_by_user_id

                # Достаем профили обоих участников
                if current_role == RoleEnum.parent:
                    parent_res = await db.execute(select(ParentProfile).where(ParentProfile.user_id == new_user.id))
                    child_res = await db.execute(select(ChildProfile).where(ChildProfile.user_id == partner_user_id))
                else:
                    parent_res = await db.execute(select(ParentProfile).where(ParentProfile.user_id == partner_user_id))
                    child_res = await db.execute(select(ChildProfile).where(ChildProfile.user_id == new_user.id))

                parent_profile = parent_res.scalar_one_or_none()
                child_profile = child_res.scalar_one_or_none()

                # Если оба профиля на месте — создаем ту самую долгожданную связь в БД!
                if parent_profile and child_profile:
                    # Импортируем модель связи внутри функции, если её нет вверху файла
                    from app.models.models import ParentChild

                    new_relation = ParentChild(
                        parent_id=parent_profile.id,
                        child_id=child_profile.id
                    )
                    db.add(new_relation)

    await db.commit()
    await db.refresh(new_user)
    return new_user