import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Response
from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.models import User, RoleEnum, StoredFile, Homework, Lesson, ParentChild, TutorDocument

router = APIRouter(prefix="/files", tags=["files"])

ALLOWED_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
}
MAX_FILE_SIZE = 15 * 1024 * 1024
MAX_UPLOADS_PER_10_MIN = 40               # защита от засорения базы
MAX_BYTES_PER_USER = 300 * 1024 * 1024    # не больше 300 МБ файлов на одного пользователя
FILE_URL_PREFIX = "/api/v1/files/"


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Загрузка файла ДЗ/ответа. Файл сохраняется в базе (а не на диске сервера,
    который на Render стирается при перезапуске) и отдаётся только тем, кому
    он нужен: автору, ученику, его родителям, репетитору занятия и админу."""
    if current_user.role not in (RoleEnum.child, RoleEnum.tutor, RoleEnum.admin):
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Разрешены PDF, DOC, DOCX, JPG, PNG, WEBP и HEIC")

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Файл пустой")
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="Файл слишком большой, максимум 15 МБ")

    if current_user.role != RoleEnum.admin:
        recent = await db.scalar(
            select(func.count(StoredFile.id)).where(
                StoredFile.owner_user_id == current_user.id,
                StoredFile.created_at >= datetime.utcnow() - timedelta(minutes=10),
            )
        )
        if (recent or 0) >= MAX_UPLOADS_PER_10_MIN:
            raise HTTPException(status_code=429, detail="Слишком много файлов подряд — попробуйте через несколько минут")
        total = await db.scalar(
            select(func.coalesce(func.sum(StoredFile.size), 0)).where(StoredFile.owner_user_id == current_user.id)
        )
        if (total or 0) + len(contents) > MAX_BYTES_PER_USER:
            raise HTTPException(status_code=400, detail="Превышен объём загруженных файлов. Обратитесь к администратору")

    file_id = uuid.uuid4().hex
    name = (file.filename or f"file{ext}")[:255]
    stored = StoredFile(
        id=file_id,
        owner_user_id=current_user.id,
        name=name,
        mime=ALLOWED_EXTENSIONS[ext],
        size=len(contents),
        data=contents,
    )
    db.add(stored)
    await db.commit()
    return {"file_url": f"{FILE_URL_PREFIX}{file_id}", "file_name": name, "mime": stored.mime, "size": stored.size}


async def _can_access(db: AsyncSession, user: User, stored: StoredFile) -> bool:
    if user.role == RoleEnum.admin or stored.owner_user_id == user.id:
        return True
    url = f"{FILE_URL_PREFIX}{stored.id}"
    res = await db.execute(
        select(Homework.child_id, Lesson.tutor_id)
        .join(Lesson, Lesson.id == Homework.lesson_id)
        .where(
            or_(
                Homework.file_url == url,
                Homework.task_files.contains(stored.id),
                Homework.submission_files.contains(stored.id),
            )
        )
    )
    if user.role == RoleEnum.tutor and user.tutor_profile:
        doc = await db.scalar(
            select(TutorDocument.id).where(TutorDocument.file_url == url, TutorDocument.tutor_id == user.tutor_profile.id)
        )
        if doc:
            return True
    for child_id, tutor_id in res.all():
        if user.role == RoleEnum.child and user.child_profile and user.child_profile.id == child_id:
            return True
        if user.role == RoleEnum.tutor and user.tutor_profile and user.tutor_profile.id == tutor_id:
            return True
        if user.role == RoleEnum.parent and user.parent_profile:
            linked = await db.scalar(
                select(ParentChild.id).where(
                    ParentChild.parent_id == user.parent_profile.id, ParentChild.child_id == child_id
                )
            )
            if linked:
                return True
    return False


@router.get("/{file_id}")
async def get_file(
    file_id: str,
    download: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not file_id.isalnum() or len(file_id) > 32:
        raise HTTPException(status_code=404, detail="Файл не найден")
    # сначала проверяем права, и только потом читаем содержимое файла из базы
    stored = await db.scalar(select(StoredFile).options(defer(StoredFile.data)).where(StoredFile.id == file_id))
    if not stored:
        raise HTTPException(status_code=404, detail="Файл не найден")
    if not await _can_access(db, current_user, stored):
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    data = await db.scalar(select(StoredFile.data).where(StoredFile.id == file_id))
    disposition = "attachment" if download else "inline"
    return Response(
        content=data,
        media_type=stored.mime,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(stored.name)}",
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )
