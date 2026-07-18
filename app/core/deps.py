from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from app.core.security import decode_token
from app.db.session import get_db
from app.models.models import User, RoleEnum, ParentProfile

bearer = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = credentials.credentials
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    result = await db.execute(
        select(User)
        .options(
            joinedload(User.tutor_profile),
            joinedload(User.child_profile),
            joinedload(User.parent_profile).selectinload(ParentProfile.children),
        )
        .where(User.id == int(user_id))
    )
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    # Auto-create missing profile for users registered before the profile-creation fix
    if user.role == RoleEnum.tutor and not user.tutor_profile:
        from app.models.models import TutorProfile
        new_profile = TutorProfile(user_id=user.id)
        db.add(new_profile)
        await db.commit()
        await db.refresh(new_profile)
        user.tutor_profile = new_profile
    elif user.role == RoleEnum.child and not user.child_profile:
        from app.models.models import ChildProfile
        new_profile = ChildProfile(user_id=user.id)
        db.add(new_profile)
        await db.commit()
        await db.refresh(new_profile)
        user.child_profile = new_profile
    elif user.role == RoleEnum.parent and not user.parent_profile:
        from app.models.models import ParentProfile as _PP
        new_profile = _PP(user_id=user.id)
        db.add(new_profile)
        await db.commit()
        await db.refresh(new_profile)
        user.parent_profile = new_profile

    return user


def require_role(*roles: RoleEnum):
    async def checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return current_user
    return checker


require_admin = require_role(RoleEnum.admin)
require_tutor = require_role(RoleEnum.tutor, RoleEnum.admin)
require_parent = require_role(RoleEnum.parent, RoleEnum.admin)
require_child = require_role(RoleEnum.child, RoleEnum.admin)
