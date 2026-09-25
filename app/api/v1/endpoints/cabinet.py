import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Body, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.models import (
    Report, Homework, Payment, Test, TestQuestion, TestAnswer,
    TestResult, Notification, Act, ParentContract, TutorContract,
    User, RoleEnum, Lesson, LessonStatus, TutorProfile, TutorDocument, ChildProfile,
    TutorPayout, ParentChild, EmailReceipt, ParentProfile, StoredFile,
)
from app.schemas.schemas import (
    ReportCreate, ReportOut, ReportUpdate,
    HomeworkCreate, HomeworkOut, HomeworkFile,
    PaymentCreate, PaymentOut,
    TestCreate, TestOut, TestResultCreate, TestResultOut,
    NotificationOut,
    ActOut, ParentContractOut, TutorContractOut,
    TutorDocumentOut,
)
from app.core.deps import get_current_user, require_admin, require_tutor
from app.core.security import get_password_hash, verify_password as _verify_password
from app.services.tutor_earnings import compute_tutor_earnings

router = APIRouter(tags=["cabinet"])


def _normalize_phone(raw: str) -> str:
    """Оставляет только цифры (без ведущего +) — чтобы "+375 29 111-11-11"
    и "375291111111" сравнивались как один и тот же номер."""
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def _user_name(user: User | None) -> str:
    if not user:
        return ""
    return f"{user.last_name or ''} {user.first_name or ''}".strip() or user.email


async def _notify(db: AsyncSession, user_id: int | None, title: str, body: str) -> None:
    if user_id:
        db.add(Notification(user_id=user_id, title=title, body=body))


async def _notify_admins(db: AsyncSession, title: str, body: str) -> None:
    result = await db.execute(select(User.id).where(User.role == RoleEnum.admin, User.is_active == True))
    for user_id in result.scalars().all():
        db.add(Notification(user_id=user_id, title=title, body=body))


# ─── Reports ──────────────────────────────────────────────────────────────────

REPORT_STATUSES = ("pending", "submitted", "approved")


def _report_query():
    return select(Report).options(
        selectinload(Report.child).selectinload(ChildProfile.user),
        selectinload(Report.tutor).selectinload(TutorProfile.user),
        selectinload(Report.subject),
        selectinload(Report.lesson),
    )


def _report_to_dict(r: Report) -> dict:
    return {
        "id": r.id,
        "tutor_id": r.tutor_id,
        "child_id": r.child_id,
        "subject_id": r.subject_id,
        "lesson_id": r.lesson_id,
        "content": r.content or "",
        "lesson_count": r.lesson_count or 5,
        "file_url": r.file_url,
        "material_score": r.material_score,
        "material_comment": r.material_comment,
        "successes": r.successes,
        "difficulties": r.difficulties,
        "homework_status": r.homework_status,
        "homework_comment": r.homework_comment,
        "engagement_score": r.engagement_score,
        "status": r.status or "approved",
        "approved_at": r.approved_at,
        "student_name": _user_name(r.child.user if r.child else None) or None,
        "tutor_name": _user_name(r.tutor.user if r.tutor else None) or None,
        "subject_name": r.subject.name if r.subject else None,
        "lesson_date": r.lesson.date if r.lesson else None,
        "hw_avg_grade": r.hw_avg_grade,
        "hw_count": r.hw_count,
        "created_at": r.created_at,
    }


def _build_report_content(r) -> str:
    """Текст отчёта из заполненных полей формы — тем же форматом, что и раньше."""
    hw = f"Домашние задания: {r.homework_status or '-'}. {r.homework_comment or ''}".strip()
    first = (
        f"Усвоение материала на основе ДЗ: {r.hw_avg_grade:g}/10."
        if getattr(r, "hw_avg_grade", None) is not None
        else f"Усвоение материала: {r.material_score or '-'}/5."
    )
    return "\n".join([
        first,
        f"Что прошли: {r.material_comment or '-'}",
        f"Успехи: {r.successes or '-'}",
        f"Зона роста: {r.difficulties or '-'}",
        hw,
        f"Активность: {r.engagement_score or '-'}/5.",
    ])


async def _hw_average(
    db: AsyncSession, tutor_id: int, child_id: int, lesson_id: Optional[int] = None, exclude_report_id: Optional[int] = None
) -> tuple[Optional[float], int]:
    """Средняя оценка за ДЗ этого репетитора у ученика за период отчёта:
    после занятия предыдущего отчёта этой пары и до занятия текущего отчёта."""
    upper = await db.scalar(select(Lesson.date).where(Lesson.id == lesson_id)) if lesson_id else None
    prev_q = (
        select(Lesson.date)
        .join(Report, Report.lesson_id == Lesson.id)
        .where(Report.tutor_id == tutor_id, Report.child_id == child_id)
    )
    if exclude_report_id:
        prev_q = prev_q.where(Report.id != exclude_report_id)
    if upper:
        prev_q = prev_q.where(Lesson.date < upper)
    lower = await db.scalar(prev_q.order_by(Lesson.date.desc()).limit(1))
    q = (
        select(func.avg(Homework.grade), func.count(Homework.id))
        .join(Lesson, Lesson.id == Homework.lesson_id)
        .where(Lesson.tutor_id == tutor_id, Homework.child_id == child_id, Homework.grade.isnot(None))
    )
    if lower:
        q = q.where(Lesson.date > lower)
    if upper:
        q = q.where(Lesson.date <= upper)
    avg, cnt = (await db.execute(q)).one()
    return (round(float(avg), 1) if avg is not None else None), int(cnt or 0)


async def _apply_hw_average(db: AsyncSession, report: Report) -> None:
    avg, cnt = await _hw_average(db, report.tutor_id, report.child_id, report.lesson_id, report.id)
    report.hw_avg_grade = avg
    report.hw_count = cnt or None
    lines = (report.content or "").split("\n")
    if avg is not None:
        report.material_score = None
        if lines and lines[0].startswith("Усвоение материала"):
            lines[0] = f"Усвоение материала на основе ДЗ: {avg:g}/10."
            report.content = "\n".join(lines)
    elif lines and lines[0].startswith("Усвоение материала на основе ДЗ"):
        lines[0] = f"Усвоение материала: {report.material_score or '-'}/5."
        report.content = "\n".join(lines)


async def _load_report(db: AsyncSession, report_id: int) -> Report | None:
    res = await db.execute(_report_query().where(Report.id == report_id))
    return res.scalars().unique().one_or_none()


# ─── Reports ──────────────────────────────────────────────────────────────────

@router.get("/reports/hw-average")
async def report_hw_average(
    child_id: int = Query(...),
    lesson_id: Optional[int] = Query(None),
    report_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_tutor),
):
    """Для формы отчёта: средняя оценка за ДЗ за период (если ДЗ задавались)."""
    tutor_id = None
    if report_id:
        rep_obj = await db.scalar(select(Report).where(Report.id == report_id))
        if rep_obj:
            tutor_id = rep_obj.tutor_id
            lesson_id = lesson_id or rep_obj.lesson_id
    if current_user.role == RoleEnum.tutor:
        if tutor_id and tutor_id != current_user.tutor_profile.id:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        tutor_id = current_user.tutor_profile.id
    if not tutor_id:
        return {"avg": None, "count": 0}
    avg, cnt = await _hw_average(db, tutor_id, child_id, lesson_id, report_id)
    return {"avg": avg, "count": cnt}


class BulkApproveBody(BaseModel):
    ids: List[int]


async def _approve(db: AsyncSession, report: Report) -> bool:
    if report.status == "pending" or not (report.content or "").strip():
        return False
    if report.status == "approved":
        return True
    report.status = "approved"
    report.approved_at = datetime.utcnow()
    parents = await db.execute(
        select(ParentProfile.user_id)
        .join(ParentChild, ParentChild.parent_id == ParentProfile.id)
        .where(ParentChild.child_id == report.child_id)
    )
    student = _user_name(report.child.user if report.child else None)
    for parent_user_id in parents.scalars().all():
        await _notify(db, parent_user_id, "Новый отчёт репетитора",
                      f"Репетитор подготовил отчёт по ученику {student}. Его можно посмотреть в разделе «Отчёты».")
    return True


@router.post("/reports/approve-bulk")
async def approve_reports_bulk(
    body: BulkApproveBody,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Одобрить сразу несколько отчётов и отправить их родителям."""
    ids = list(dict.fromkeys(body.ids))[:200]
    if not ids:
        return {"approved": [], "skipped": []}
    res = await db.execute(_report_query().where(Report.id.in_(ids)))
    approved, skipped = [], []
    for report in res.scalars().unique().all():
        (approved if await _approve(db, report) else skipped).append(report.id)
    await db.commit()
    return {"approved": approved, "skipped": skipped}


@router.get("/reports", response_model=List[ReportOut])
async def list_reports(
    child_id: Optional[int] = Query(None),
    tutor_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = _report_query()
    if current_user.role == RoleEnum.tutor and current_user.tutor_profile:
        q = q.where(Report.tutor_id == current_user.tutor_profile.id)
        if child_id:
            q = q.where(Report.child_id == child_id)
    elif current_user.role == RoleEnum.child and current_user.child_profile:
        q = q.where(Report.child_id == current_user.child_profile.id, Report.status == "approved")
    elif current_user.role == RoleEnum.parent and current_user.parent_profile:
        child_ids = [pc.child_id for pc in current_user.parent_profile.children]
        if child_id and child_id not in child_ids:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        if not child_ids:
            return []
        # Родитель видит только отчёты, одобренные администратором
        q = q.where(
            Report.child_id == child_id if child_id else Report.child_id.in_(child_ids),
            Report.status == "approved",
        )
    elif current_user.role == RoleEnum.admin:
        if child_id:
            q = q.where(Report.child_id == child_id)
        if tutor_id:
            q = q.where(Report.tutor_id == tutor_id)
    else:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    if status in REPORT_STATUSES and current_user.role in (RoleEnum.admin, RoleEnum.tutor):
        q = q.where(Report.status == status)
    result = await db.execute(q.order_by(Report.created_at.desc()))
    return [_report_to_dict(r) for r in result.scalars().unique().all()]


@router.post("/reports", response_model=ReportOut, status_code=201)
async def create_report(
    data: ReportCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_tutor),
):
    if current_user.role == RoleEnum.tutor:
        teaches = await db.scalar(
            select(func.count(Lesson.id)).where(
                Lesson.tutor_id == current_user.tutor_profile.id,
                Lesson.child_id == data.child_id,
            )
        )
        if not teaches:
            raise HTTPException(status_code=403, detail="Можно писать отчёты только по своим ученикам")
    elif not current_user.tutor_profile:
        raise HTTPException(status_code=400, detail="Отчёт может создать только репетитор")
    if not (data.content or "").strip():
        raise HTTPException(status_code=400, detail="Заполните отчёт")

    report = None
    if data.lesson_id:
        # По этому занятию уже есть отложенный/неодобренный отчёт — заполняем его, а не создаём второй
        report = (await db.execute(
            select(Report)
            .where(Report.lesson_id == data.lesson_id, Report.tutor_id == current_user.tutor_profile.id, Report.status != "approved")
            .order_by(Report.id)
        )).scalars().first()
        if not report:
            already_approved = await db.scalar(
                select(func.count(Report.id)).where(
                    Report.lesson_id == data.lesson_id, Report.tutor_id == current_user.tutor_profile.id, Report.status == "approved"
                )
            )
            if already_approved:
                raise HTTPException(status_code=400, detail="Отчёт по этому занятию уже одобрен")
    if report:
        for field, value in data.model_dump().items():
            setattr(report, field, value)
        report.status = "submitted"
    else:
        report = Report(**data.model_dump(), tutor_id=current_user.tutor_profile.id, status="submitted")
        db.add(report)
        await db.flush()
    await _apply_hw_average(db, report)
    child = await db.scalar(select(ChildProfile).where(ChildProfile.id == data.child_id).options(selectinload(ChildProfile.user)))
    await _notify_admins(
        db,
        "Новый отчёт на проверку",
        f"Репетитор {_user_name(current_user)} заполнил отчёт по ученику {_user_name(child.user if child else None)}. Проверьте и одобрите его в разделе «Отчёты».",
    )
    await db.commit()
    return _report_to_dict(await _load_report(db, report.id))


@router.patch("/reports/{report_id}", response_model=ReportOut)
async def update_report(
    report_id: int,
    data: ReportUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_tutor),
):
    """Репетитор заполняет/исправляет свой отложенный или ещё не одобренный
    отчёт; админ может исправить любой отчёт."""
    report = await db.scalar(select(Report).where(Report.id == report_id))
    if not report:
        raise HTTPException(status_code=404, detail="Отчёт не найден")
    is_admin = current_user.role == RoleEnum.admin
    if not is_admin:
        if not current_user.tutor_profile or report.tutor_id != current_user.tutor_profile.id:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        if report.status == "approved":
            raise HTTPException(status_code=400, detail="Отчёт уже одобрен администратором — изменить его может только администратор")
    updates = data.model_dump(exclude_none=True)
    for field, value in updates.items():
        setattr(report, field, value)
    form_fields = ("material_comment", "successes", "difficulties", "material_score", "engagement_score", "homework_status")
    if not (updates.get("content") or "").strip() and any(f in updates for f in form_fields):
        report.content = _build_report_content(report)
    leaving_pending = report.status == "pending"
    if leaving_pending and not all((getattr(report, f) or "").strip() for f in ("material_comment", "successes", "difficulties")):
        raise HTTPException(status_code=400, detail="Заполните основные поля отчёта: что прошли, успехи и зону роста")
    if not (report.content or "").strip():
        raise HTTPException(status_code=400, detail="Отчёт не может быть пустым")
    # Средняя оценка за ДЗ пересчитывается при каждом сохранении отчёта,
    # кроме уже отправленных родителям (их текст не меняем задним числом)
    if report.status != "approved":
        await _apply_hw_average(db, report)
    if not is_admin:
        was_pending = report.status == "pending"
        report.status = "submitted"
        if was_pending:
            await _notify_admins(db, "Новый отчёт на проверку",
                                 f"Репетитор {_user_name(current_user)} заполнил отложенный отчёт. Проверьте его в разделе «Отчёты».")
    elif report.status == "pending":
        report.status = "submitted"
    await db.commit()
    return _report_to_dict(await _load_report(db, report_id))


@router.post("/reports/{report_id}/approve", response_model=ReportOut)
async def approve_report(
    report_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Админ одобряет отчёт — после этого он появляется в кабинете родителя."""
    report = await _load_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Отчёт не найден")
    if not await _approve(db, report):
        raise HTTPException(status_code=400, detail="Отчёт ещё не заполнен репетитором")
    await db.commit()
    return _report_to_dict(await _load_report(db, report_id))


@router.delete("/reports/{report_id}", status_code=204)
async def delete_report(
    report_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    report = await db.scalar(select(Report).where(Report.id == report_id))
    if not report:
        raise HTTPException(status_code=404, detail="Отчёт не найден")
    await db.delete(report)
    await db.commit()
    return Response(status_code=204)


# ─── Tutor documents (полученные от админа) ────────────────────────────────────

@router.get("/tutor-documents", response_model=List[TutorDocumentOut])
async def list_my_tutor_documents(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_tutor),
):
    """Репетитор видит документы, присланные ему админом."""
    result = await db.execute(
        select(TutorDocument)
        .where(TutorDocument.tutor_id == current_user.tutor_profile.id)
        .order_by(TutorDocument.created_at.desc())
    )
    return result.scalars().all()


# ─── Homeworks ────────────────────────────────────────────────────────────────

def _json_files(raw) -> list:
    import json
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    out = []
    for f in data if isinstance(data, list) else []:
        if isinstance(f, dict) and f.get("url"):
            out.append({"url": str(f["url"])[:500], "name": (f.get("name") or "")[:255] or None, "mime": (f.get("mime") or "")[:150] or None})
    return out


async def _check_file_refs(db: AsyncSession, user: User, files: list, already: list) -> None:
    """В ДЗ можно прикрепить только файлы, загруженные этим же пользователем
    (или уже прикреплённые к этому ДЗ) — чужой файл по ссылке не «присвоить»."""
    known = {f.get("url") for f in already}
    ids = []
    for f in files:
        url = f["url"]
        if url in known:
            continue
        if url.startswith("/uploads/") and ".." not in url:
            continue  # старые файлы, загруженные до перехода на хранение в базе
        if not url.startswith("/api/v1/files/"):
            raise HTTPException(status_code=400, detail="Недопустимый файл")
        ids.append(url[len("/api/v1/files/"):])
    if not ids or user.role == RoleEnum.admin:
        return
    own = await db.scalar(
        select(func.count(StoredFile.id)).where(StoredFile.id.in_(ids), StoredFile.owner_user_id == user.id)
    )
    if own != len(set(ids)):
        raise HTTPException(status_code=400, detail="Недопустимый файл")


def _homework_to_dict(hw: Homework) -> dict:
    lesson = hw.lesson
    task_files = _json_files(hw.task_files)
    if not task_files and hw.file_url and hw.file_url.startswith("/"):
        task_files = [{"url": hw.file_url, "name": None, "mime": None}]
    submission_files = _json_files(hw.submission_files)
    if not submission_files and hw.submission_url and hw.submission_url.startswith("/"):
        submission_files = [{"url": hw.submission_url, "name": None, "mime": None}]
    return {
        "id": hw.id,
        "lesson_id": hw.lesson_id,
        "child_id": hw.child_id,
        "description": hw.description,
        "file_url": hw.file_url,
        "submission_url": hw.submission_url,
        "is_done": bool(hw.is_done),
        "created_at": hw.created_at,
        "task_files": task_files,
        "submission_files": submission_files,
        "submitted_at": hw.submitted_at,
        "grade": hw.grade,
        "tutor_comment": hw.tutor_comment,
        "checked_at": hw.checked_at,
        "lesson_date": lesson.date if lesson else None,
        "subject_name": lesson.subject.name if lesson and lesson.subject else None,
        "tutor_name": _user_name(lesson.tutor.user if lesson and lesson.tutor else None) or None,
        "student_name": _user_name(hw.child.user if hw.child else None) or None,
    }


def _homework_query():
    return select(Homework).options(
        selectinload(Homework.lesson).selectinload(Lesson.subject),
        selectinload(Homework.lesson).selectinload(Lesson.tutor).selectinload(TutorProfile.user),
        selectinload(Homework.child).selectinload(ChildProfile.user),
    )


async def _load_homework(db: AsyncSession, hw_id: int) -> Homework | None:
    res = await db.execute(_homework_query().where(Homework.id == hw_id))
    return res.scalars().unique().one_or_none()


@router.get("/homeworks", response_model=List[HomeworkOut])
async def list_homeworks(
    child_id: Optional[int] = Query(None),
    lesson_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = _homework_query()
    if current_user.role == RoleEnum.child and current_user.child_profile:
        q = q.where(Homework.child_id == current_user.child_profile.id)
    elif current_user.role == RoleEnum.parent and current_user.parent_profile:
        child_ids = [pc.child_id for pc in current_user.parent_profile.children]
        if child_id and child_id not in child_ids:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        q = q.where(Homework.child_id == child_id if child_id else Homework.child_id.in_(child_ids))
    elif current_user.role == RoleEnum.tutor and current_user.tutor_profile:
        lesson_ids = select(Lesson.id).where(Lesson.tutor_id == current_user.tutor_profile.id)
        q = q.where(Homework.lesson_id.in_(lesson_ids))
        if child_id:
            q = q.where(Homework.child_id == child_id)
    elif current_user.role == RoleEnum.admin:
        if child_id:
            q = q.where(Homework.child_id == child_id)
    else:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    if lesson_id:
        q = q.where(Homework.lesson_id == lesson_id)
    result = await db.execute(q.order_by(Homework.created_at.desc(), Homework.id.desc()))
    return [_homework_to_dict(h) for h in result.scalars().unique().all()]


@router.post("/homeworks", response_model=HomeworkOut, status_code=201)
async def create_homework(
    data: HomeworkCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_tutor),
):
    import json
    lesson = await db.scalar(
        select(Lesson)
        .where(Lesson.id == data.lesson_id)
        .options(selectinload(Lesson.child).selectinload(ChildProfile.user))
    )
    if not lesson or lesson.child_id != data.child_id:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    if current_user.role == RoleEnum.tutor and lesson.tutor_id != current_user.tutor_profile.id:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    payload = data.model_dump()
    files = (payload.pop("task_files", None) or [])[:20]
    if not files and payload.get("file_url"):
        files = [{"url": payload["file_url"], "name": None, "mime": None}]
    await _check_file_refs(db, current_user, files, [])
    if files and not payload.get("file_url"):
        payload["file_url"] = files[0]["url"]
    hw = Homework(**payload, task_files=json.dumps(files, ensure_ascii=False) if files else None)
    db.add(hw)
    await _notify(
        db,
        lesson.child.user_id if lesson.child else None,
        "Новое домашнее задание",
        f"Репетитор {_user_name(current_user)} добавил ДЗ по занятию {lesson.date}.",
    )
    await db.commit()
    return _homework_to_dict(await _load_homework(db, hw.id))


class HomeworkSubmitBody(BaseModel):
    submission_url: Optional[str] = None
    files: Optional[List[HomeworkFile]] = None


@router.patch("/homeworks/{hw_id}/submit", response_model=HomeworkOut)
async def submit_homework(
    hw_id: int,
    body: Optional[HomeworkSubmitBody] = Body(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Ответ ученика: список файлов/фото. Можно добавлять новые и удалять
    загруженные — пока репетитор не выставил оценку. Если файлов не осталось,
    ДЗ снова считается невыполненным."""
    import json
    hw = await _load_homework(db, hw_id)
    if not hw:
        raise HTTPException(status_code=404, detail="Домашнее задание не найдено")
    if current_user.role == RoleEnum.child and current_user.child_profile:
        if hw.child_id != current_user.child_profile.id:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
    elif current_user.role != RoleEnum.admin:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    if hw.grade is not None and current_user.role != RoleEnum.admin:
        raise HTTPException(status_code=400, detail="Задание уже проверено репетитором — изменить ответ нельзя")

    was_done = bool(hw.is_done)
    already = _json_files(hw.submission_files)
    if not already and hw.submission_url and hw.submission_url != "done":
        already = [{"url": hw.submission_url}]
    if body and body.files is None and body.submission_url and body.submission_url.startswith(("/api/v1/files/", "/uploads/")):
        # старые версии кабинета присылают один файл строкой — приводим к списку
        body.files = [HomeworkFile(url=body.submission_url)]
    if body and body.files is not None:
        files = [f.model_dump() for f in body.files][:20]
        await _check_file_refs(db, current_user, files, already)
        hw.submission_files = json.dumps(files, ensure_ascii=False) if files else None
        hw.submission_url = files[0]["url"] if files else None
        hw.is_done = bool(files)
    else:
        # старый вариант «Отметить выполненным» (без файлов)
        if body and body.submission_url and body.submission_url != "done":
            hw.submission_url = body.submission_url[:500]
        hw.is_done = True
    hw.submitted_at = datetime.utcnow() if hw.is_done else None

    if hw.is_done and not was_done:
        tutor_user_id = hw.lesson.tutor.user_id if hw.lesson and hw.lesson.tutor else None
        await _notify(db, tutor_user_id, "Ответ на домашнее задание",
                      f"Ученик {_user_name(current_user)} прислал ответ на ДЗ от {hw.lesson.date if hw.lesson else ''}. Проверьте его во вкладке «Домашние задания».")
    await db.commit()
    return _homework_to_dict(await _load_homework(db, hw_id))


class HomeworkGradeBody(BaseModel):
    grade: int
    comment: Optional[str] = None


@router.patch("/homeworks/{hw_id}/grade", response_model=HomeworkOut)
async def grade_homework(
    hw_id: int,
    body: HomeworkGradeBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_tutor),
):
    """Репетитор проверяет ДЗ и ставит оценку по 10-балльной шкале."""
    if body.grade < 1 or body.grade > 10:
        raise HTTPException(status_code=400, detail="Оценка должна быть от 1 до 10")
    hw = await _load_homework(db, hw_id)
    if not hw:
        raise HTTPException(status_code=404, detail="Домашнее задание не найдено")
    if current_user.role == RoleEnum.tutor and (not hw.lesson or hw.lesson.tutor_id != current_user.tutor_profile.id):
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    hw.grade = body.grade
    hw.tutor_comment = (body.comment or "").strip() or None
    hw.checked_at = datetime.utcnow()
    await _notify(db, hw.child.user_id if hw.child else None, "ДЗ проверено",
                  f"Репетитор проверил домашнее задание от {hw.lesson.date if hw.lesson else ''}: оценка {body.grade}/10.")
    await db.commit()
    return _homework_to_dict(await _load_homework(db, hw_id))


# ─── Payments ─────────────────────────────────────────────────────────────────

@router.get("/payments", response_model=List[PaymentOut])
async def list_payments(
    child_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(Payment)
    if current_user.role == RoleEnum.parent and current_user.parent_profile:
        q = q.where(Payment.parent_id == current_user.parent_profile.id)
    elif current_user.role == RoleEnum.admin:
        if child_id:
            q = q.where(Payment.child_id == child_id)
    else:
        # репетитор/ученик раньше получали платежи всех семей
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    result = await db.execute(q.order_by(Payment.created_at.desc()))
    return result.scalars().all()


@router.post("/payments", response_model=PaymentOut, status_code=201)
async def create_payment(
    data: PaymentCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    payment = Payment(**data.model_dump())
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    return payment


@router.patch("/payments/{payment_id}/mark-paid")
async def mark_payment_paid(
    payment_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    from datetime import datetime
    result = await db.execute(select(Payment).where(Payment.id == payment_id))
    payment = result.scalar_one_or_none()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    payment.is_paid = True
    payment.paid_at = datetime.utcnow()
    await db.commit()
    return {"ok": True}


@router.get("/finance/parent")
async def get_parent_finance(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Реальная сумма оплаченных/неоплаченных занятий и задолженность для
    ЛК родителя — считается той же логикой, что и админский finance-report
    (включая учёт «семейных» платежей на нескольких детей одного плательщика),
    чтобы цифры совпадали с СРМ."""
    from app.services.finance_report import compute_finance_rows

    if current_user.role != RoleEnum.parent or not current_user.parent_profile:
        raise HTTPException(status_code=403, detail="Only parents can view this")

    child_ids_res = await db.execute(
        select(ParentChild.child_id).where(ParentChild.parent_id == current_user.parent_profile.id)
    )
    child_ids = [row[0] for row in child_ids_res.all()]
    if not child_ids:
        return {"lessons_paid": 0, "lessons_conducted": 0, "debt": 0.0, "receipts": []}

    rows = await compute_finance_rows(db, child_ids=child_ids)
    lessons_paid = sum(r.lessons_paid for r in rows)
    lessons_conducted = sum(r.lessons_conducted for r in rows)
    debt = sum(max(0.0, r.lessons_conducted * r.lesson_price - r.amount_paid) for r in rows)

    receipts_res = await db.execute(
        select(EmailReceipt)
        .where(EmailReceipt.child_id.in_(child_ids))
        .order_by(EmailReceipt.payment_date.desc(), EmailReceipt.created_at.desc())
    )
    receipts = [
        {
            "id": r.id,
            "amount": r.amount,
            "payment_date": r.payment_date,
            "created_at": r.created_at,
        }
        for r in receipts_res.scalars().all()
    ]

    return {
        "lessons_paid": lessons_paid,
        "lessons_conducted": lessons_conducted,
        "debt": round(debt, 2),
        "receipts": receipts,
    }


# ─── Tests ────────────────────────────────────────────────────────────────────

@router.get("/tests", response_model=List[TestOut])
async def list_tests(
    subject_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy.orm import selectinload
    q = select(Test).options(
        selectinload(Test.questions).selectinload(TestQuestion.answers)
    ).where(Test.is_active == True)
    if current_user.role == RoleEnum.tutor and current_user.tutor_profile:
        q = q.where(Test.tutor_id == current_user.tutor_profile.id)
    if subject_id:
        q = q.where(Test.subject_id == subject_id)
    result = await db.execute(q)
    return result.scalars().all()


@router.post("/tests", response_model=TestOut, status_code=201)
async def create_test(
    data: TestCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_tutor),
):
    test = Test(
        tutor_id=current_user.tutor_profile.id,
        subject_id=data.subject_id,
        title=data.title,
        description=data.description,
    )
    db.add(test)
    await db.flush()

    for q_data in data.questions:
        question = TestQuestion(
            test_id=test.id,
            text=q_data.text,
            question_type=q_data.question_type,
            order=q_data.order,
        )
        db.add(question)
        await db.flush()
        for a_data in q_data.answers:
            answer = TestAnswer(question_id=question.id, text=a_data.text, is_correct=a_data.is_correct)
            db.add(answer)

    await db.commit()
    await db.refresh(test)
    return test


@router.post("/tests/{test_id}/results", response_model=TestResultOut, status_code=201)
async def submit_test_result(
    test_id: int,
    data: TestResultCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != RoleEnum.admin and not (
        current_user.role == RoleEnum.child
        and current_user.child_profile
        and current_user.child_profile.id == data.child_id
    ):
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    result_obj = TestResult(test_id=test_id, child_id=data.child_id, answers_json=data.answers_json)
    db.add(result_obj)
    await db.commit()
    await db.refresh(result_obj)
    return result_obj


@router.get("/tests/{test_id}/results", response_model=List[TestResultOut])
async def get_test_results(
    test_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != RoleEnum.admin:
        test = await db.scalar(select(Test).where(Test.id == test_id))
        if not (
            current_user.role == RoleEnum.tutor
            and current_user.tutor_profile
            and test is not None
            and test.tutor_id == current_user.tutor_profile.id
        ):
            raise HTTPException(status_code=403, detail="Недостаточно прав")
    result = await db.execute(select(TestResult).where(TestResult.test_id == test_id))
    return result.scalars().all()


# ─── Notifications ────────────────────────────────────────────────────────────

@router.get("/notifications", response_model=List[NotificationOut])
async def list_notifications(
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Раньше отдавались ВСЕ уведомления (у админа их тысячи) — это тормозило
    # каждое открытие кабинета. Теперь только последние.
    result = await db.execute(
        select(Notification)
        .where(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


@router.get("/notifications/unread-count")
async def unread_notifications_count(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    count = await db.scalar(
        select(func.count(Notification.id)).where(Notification.user_id == current_user.id, Notification.is_read == False)
    ) or 0
    return {"count": count}


@router.patch("/notifications/read-all")
async def mark_all_notifications_read(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy import update as sa_update
    await db.execute(
        sa_update(Notification)
        .where(Notification.user_id == current_user.id, Notification.is_read == False)
        .values(is_read=True)
    )
    await db.commit()
    return {"ok": True}


@router.patch("/notifications/{notif_id}/read")
async def mark_read(
    notif_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.models.models import Notification
    result = await db.execute(
        select(Notification).where(Notification.id == notif_id, Notification.user_id == current_user.id)
    )
    notif = result.scalar_one_or_none()
    if not notif:
        raise HTTPException(status_code=404, detail="Not found")
    notif.is_read = True
    await db.commit()
    return {"ok": True}


# ─── Acts ─────────────────────────────────────────────────────────────────────

@router.get("/acts", response_model=List[ActOut])
async def list_acts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(Act)
    if current_user.role == RoleEnum.tutor and current_user.tutor_profile:
        q = q.where(Act.tutor_id == current_user.tutor_profile.id)
    elif current_user.role != RoleEnum.admin:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    result = await db.execute(q.order_by(Act.created_at.desc()))
    return result.scalars().all()


@router.post("/tutor/acts", response_model=ActOut, status_code=201)
async def upload_signed_act(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_tutor),
):
    """Репетитор загружает подписанный акт (PDF). Если за текущий месяц ещё нет
    акта — создаём его автоматически; если есть неподписанный — обновляем его."""
    ext = Path(file.filename or "").suffix.lower()
    if ext != ".pdf":
        raise HTTPException(status_code=400, detail="Разрешены только PDF-файлы")
    contents = await file.read()
    if len(contents) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Файл слишком большой (максимум 15 МБ)")

    upload_dir = Path(__file__).resolve().parents[3] / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{ext}"
    (upload_dir / stored_name).write_bytes(contents)
    file_url = f"/uploads/{stored_name}"

    tutor_id = current_user.tutor_profile.id
    today = date.today()
    period_start = today.replace(day=1)
    next_month = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    period_end = next_month - timedelta(days=1)

    result = await db.execute(
        select(Act)
        .where(Act.tutor_id == tutor_id, Act.signed_url.is_(None))
        .order_by(Act.created_at.desc())
    )
    act = result.scalars().first()

    if act:
        act.signed_url = file_url
    else:
        lessons_res = await db.execute(
            select(func.count(Lesson.id)).where(
                Lesson.tutor_id == tutor_id,
                Lesson.date >= period_start,
                Lesson.date <= period_end,
            )
        )
        lessons_count = lessons_res.scalar() or 0
        act = Act(
            tutor_id=tutor_id,
            period_start=period_start,
            period_end=period_end,
            lessons_count=lessons_count,
            total_amount=0,
            signed_url=file_url,
        )
        db.add(act)

    await db.commit()
    await db.refresh(act)
    return act


@router.get("/tutor/act/blank")
async def download_act_blank(_: User = Depends(require_tutor)):
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj<<>>endobj\n"
        b"2 0 obj<< /Length 44 >>stream\n"
        b"BT /F1 18 Tf 72 720 Td (Pifagor act blank) Tj ET\n"
        b"endstream endobj\n"
        b"3 0 obj<< /Type /Page /Parent 4 0 R /Contents 2 0 R >>endobj\n"
        b"4 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n"
        b"5 0 obj<< /Type /Catalog /Pages 4 0 R >>endobj\n"
        b"trailer<< /Root 5 0 R >>\n%%EOF"
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="pifagor-act-blank.pdf"'},
    )


# ─── Contracts ────────────────────────────────────────────────────────────────

@router.get("/contracts/parent", response_model=List[ParentContractOut])
async def list_parent_contracts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(ParentContract)
    if current_user.role == RoleEnum.parent and current_user.parent_profile:
        q = q.where(ParentContract.parent_id == current_user.parent_profile.id)
    elif current_user.role != RoleEnum.admin:
        # раньше репетитор/ученик получали ВСЕ договоры родителей с личными данными
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    result = await db.execute(q)
    return result.scalars().all()


@router.get("/profile/parent")
async def get_parent_profile(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Данные для вкладки "Профиль" в ЛК родителя: аккаунт + данные,
    распознанные из договора (ФИО, телефон, email, адрес, номер и суммы
    договора). Файл договора больше нигде не хранится и не отдаётся —
    только эти данные."""
    if current_user.role != RoleEnum.parent or not current_user.parent_profile:
        raise HTTPException(status_code=403, detail="Only parents can view this")

    result = await db.execute(
        select(ParentContract)
        .where(ParentContract.parent_id == current_user.parent_profile.id)
        .order_by(ParentContract.created_at.desc())
    )
    contract = result.scalars().first()

    return {
        "account": {
            "first_name": current_user.first_name,
            "last_name": current_user.last_name,
            "middle_name": current_user.middle_name,
            "email": current_user.email,
            "phone": current_user.phone,
        },
        "contract": {
            "contract_number": contract.contract_number if contract else None,
            "parent_full_name": contract.parent_full_name if contract else None,
            "parent_phone": contract.parent_phone if contract else None,
            "parent_email": contract.parent_email if contract else None,
            "city": contract.city if contract else None,
            "street": contract.street if contract else None,
            "house": contract.house if contract else None,
            "start_date": contract.start_date if contract else None,
            "end_date": contract.end_date if contract else None,
        } if contract else None,
    }


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
async def change_password(
    body: ChangePasswordBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not _verify_password(body.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Неверный текущий пароль")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="Пароль минимум 6 символов")
    current_user.hashed_password = get_password_hash(body.new_password)
    await db.commit()
    return {"ok": True}


class ForgotPasswordRequestBody(BaseModel):
    phone: str
    email: str


class ForgotPasswordConfirmBody(BaseModel):
    phone: str
    email: str
    code: str
    new_password: str


# Коды восстановления пароля — короткоживущие (15 минут), поэтому хранить
# их в памяти процесса достаточно: не переживает рестарт/масштабирование
# на несколько инстансов, но для этого проекта это приемлемо и не требует
# отдельной таблицы в БД. {user_id: {"code": str, "expires_at": datetime, "attempts": int}}
_reset_codes: dict[int, dict] = {}


async def _find_user_by_phone_email(db: AsyncSession, phone: str, email: str) -> User | None:
    phone_digits = _normalize_phone(phone)
    email_norm = (email or "").strip().lower()
    if not phone_digits or not email_norm:
        return None
    result = await db.execute(select(User).where(func.lower(User.email) == email_norm))
    user = result.scalar_one_or_none()
    if not user or not user.phone or _normalize_phone(user.phone) != phone_digits:
        return None
    return user


@router.post("/auth/forgot-password/request")
async def forgot_password_request(
    body: ForgotPasswordRequestBody,
    db: AsyncSession = Depends(get_db),
):
    """Шаг 1 восстановления пароля: если телефон+email совпадают с
    учётной записью, на её email отправляется 6-значный код. Ответ
    одинаковый независимо от того, найден пользователь или нет — чтобы
    нельзя было по ответу узнавать, привязана ли конкретная пара
    телефон+email к какому-то аккаунту."""
    import secrets
    from app.services.email_notification import send_password_reset_code

    user = await _find_user_by_phone_email(db, body.phone, body.email)
    if user:
        code = f"{secrets.randbelow(1_000_000):06d}"
        _reset_codes[user.id] = {
            "code": code,
            "expires_at": datetime.utcnow() + timedelta(minutes=15),
            "attempts": 0,
        }
        send_password_reset_code(user.email, code)

    return {"ok": True, "message": "Если данные верны, код отправлен на почту, привязанную к аккаунту."}


@router.post("/auth/forgot-password")
async def forgot_password_confirm(
    body: ForgotPasswordConfirmBody,
    db: AsyncSession = Depends(get_db),
):
    """Шаг 2: подтверждение кода из письма и установка нового пароля."""
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="Пароль минимум 6 символов")

    user = await _find_user_by_phone_email(db, body.phone, body.email)
    if not user:
        raise HTTPException(status_code=400, detail="Неверные данные — проверьте телефон и email")

    entry = _reset_codes.get(user.id)
    if not entry:
        raise HTTPException(status_code=400, detail="Сначала запросите код — нажмите «Забыли пароль» ещё раз")
    if datetime.utcnow() > entry["expires_at"]:
        _reset_codes.pop(user.id, None)
        raise HTTPException(status_code=400, detail="Код истёк, запросите новый")
    entry["attempts"] += 1
    if entry["attempts"] > 5:
        _reset_codes.pop(user.id, None)
        raise HTTPException(status_code=400, detail="Слишком много попыток, запросите новый код")
    if body.code.strip() != entry["code"]:
        raise HTTPException(status_code=400, detail="Неверный код")

    user.hashed_password = get_password_hash(body.new_password)
    await db.commit()
    _reset_codes.pop(user.id, None)
    return {"ok": True}


@router.get("/contracts/tutor", response_model=List[TutorContractOut])
async def list_tutor_contracts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(TutorContract)
    if current_user.role == RoleEnum.tutor and current_user.tutor_profile:
        q = q.where(TutorContract.tutor_id == current_user.tutor_profile.id)
    elif current_user.role != RoleEnum.admin:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    result = await db.execute(q)
    return result.scalars().all()


@router.post("/tutor/contract/{contract_id}/signed")
async def upload_tutor_signed_contract(
    contract_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_tutor),
):
    result = await db.execute(select(TutorContract).where(TutorContract.id == contract_id))
    contract = result.scalar_one_or_none()
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")
    if not current_user.tutor_profile or contract.tutor_id != current_user.tutor_profile.id:
        raise HTTPException(status_code=403, detail="This contract belongs to another tutor")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".pdf", ".jpg", ".jpeg", ".png"}:
        raise HTTPException(status_code=400, detail="Allowed files: PDF, JPG, PNG")

    contents = await file.read()
    upload_dir = Path(__file__).resolve().parents[3] / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{ext}"
    (upload_dir / stored_name).write_bytes(contents)
    file_url = f"/uploads/{stored_name}"

    contract.signed_file_url = file_url
    contract.signed_at = datetime.utcnow()
    await db.commit()
    return {"ok": True, "file_url": file_url}


@router.get("/tutor/contract/{contract_id}/file")
async def download_tutor_contract(
    contract_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(TutorContract).where(TutorContract.id == contract_id))
    contract = result.scalar_one_or_none()
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")
    if current_user.role == RoleEnum.tutor:
        if not current_user.tutor_profile or contract.tutor_id != current_user.tutor_profile.id:
            raise HTTPException(status_code=403, detail="This contract belongs to another tutor")
    elif current_user.role != RoleEnum.admin:
        raise HTTPException(status_code=403, detail="Only tutor or admin can download this contract")

    file_url = contract.signed_file_url or contract.file_url
    if not file_url:
        raise HTTPException(status_code=404, detail="Contract file not found")
    if file_url.startswith("/uploads/"):
        file_path = Path(__file__).resolve().parents[3] / file_url.lstrip("/")
        if file_path.exists():
            return FileResponse(file_path, media_type="application/pdf", filename=file_path.name)
    return RedirectResponse(file_url)



# ─── Tutor Finance Stats ───────────────────────────────────────────────────────

@router.get("/tutor/finance")
async def get_tutor_finance(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Tutor finance stats: lessons done, acts uploaded, earnings estimate."""
    if current_user.role not in (RoleEnum.tutor, RoleEnum.admin):
        raise HTTPException(status_code=403, detail="Only tutors can access this")

    if not current_user.tutor_profile:
        return {"lessons_done": 0, "earnings": 0, "acts_count": 0}

    tutor_id = current_user.tutor_profile.id

    # Count acts
    acts_result = await db.execute(
        select(func.count(Act.id)).where(Act.tutor_id == tutor_id)
    )
    acts_count = acts_result.scalar() or 0

    # Заработок с учётом истории ставок (если ставку меняли с конкретной
    # даты — каждое занятие считается по той ставке, что действовала на
    # его дату, а не по текущей).
    total_earned, lessons_done = await compute_tutor_earnings(db, tutor_id)
    rate = current_user.tutor_profile.rate_per_hour or 0

    paid_result = await db.execute(
        select(func.sum(TutorPayout.amount)).where(TutorPayout.tutor_id == tutor_id)
    )
    total_paid = paid_result.scalar() or 0
    earnings = round(max(0.0, total_earned - total_paid), 2)

    # Количество занятий, которое показываем рядом с суммой — считается от самой
    # суммы (сумма / ставка), а не общим числом проведённых занятий за всю
    # историю, чтобы цифры были согласованы между собой.
    unpaid_lessons_count = round(earnings / rate) if rate else lessons_done

    return {
        "lessons_done": unpaid_lessons_count,
        "earnings": earnings,
        "acts_count": acts_count,
    }
