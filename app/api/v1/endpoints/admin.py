import secrets
import json
import uuid
from pathlib import Path
from typing import List, Optional
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, status, Query, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, cast, Date, or_
from sqlalchemy.orm import joinedload
from pydantic import BaseModel  # 🌟 Добавили для Pydantic схемы

from app.db.session import get_db
from app.core.deps import require_admin
from app.core.security import get_password_hash
from app.services.contract_parser import parse_contract_docx
from app.services.email_parser import _find_child_by_payer_name
from app.models.models import (
    InviteCode, RoleEnum, Lesson, LessonStatus, Notification,
    ChildProfile, User, EmailReceipt, ParentProfile, ParentChild,  # 🌟 Добавили профили
    TutorProfile, TutorSubject, Subject, TutorDocument, TutorContract, Act,
    Homework, Report, Material, ParentContract, Payment, Comment, TestResult, Review,
    TutorPayout,
)
from app.schemas.schemas import (
    InviteCodeCreate, InviteCodeResponse,
    EmailReceiptOut, StudentFinanceRow,
    TutorProfileOut, AdminTutorUpdate,
    TutorDocumentCreate, TutorDocumentOut,
    ActOut, TutorPayoutCreate, TutorPayoutOut,
)

router = APIRouter()
MAX_INVITE_DESCRIPTION_LENGTH = 120


# Pydantic схема для ручного бинда
class BaseParentChildLink(BaseModel):
    parent_id: int
    child_id: int


class AdminStudentUpdate(BaseModel):
    lesson_price: Optional[float] = None
    grade: Optional[int] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    status: Optional[str] = None
    lessons_per_week: Optional[int] = None
    notes: Optional[str] = None
    channel: Optional[str] = None
    parent_name: Optional[str] = None
    parent_phone: Optional[str] = None
    subjects: Optional[List[str]] = None
    tutors: Optional[List[str]] = None
    contract_label: Optional[str] = None


class TutorWorkDocumentCreate(BaseModel):
    kind: str
    title: str
    file_url: str


def generate_random_code(prefix: str) -> str:
    return f"PIF-{prefix.upper()}-{secrets.token_hex(3).upper()}"


def _split_name(raw: str | None) -> tuple[str, str]:
    parts = (raw or "Новый ученик").strip().split()
    if len(parts) >= 2:
        return parts[1], parts[0]
    return parts[0] if parts else "Новый", "ученик"


def _clean_invite_description(description: Optional[str]) -> Optional[str]:
    if description is None:
        return None
    clean = description.strip()
    if len(clean) > MAX_INVITE_DESCRIPTION_LENGTH:
        raise HTTPException(status_code=400, detail="Описание кода слишком длинное")
    return clean or None


def _is_suspicious_invite_description(description: Optional[str]) -> bool:
    if not description:
        return False
    clean = description.strip()
    if "admin" in clean.lower() or "админ" in clean.lower():
        return True
    if len(clean) > MAX_INVITE_DESCRIPTION_LENGTH:
        return True
    allowed_punctuation = {" ", "-", "'", ".", "ё", "Ё"}
    suspicious = 0
    for ch in clean:
        code = ord(ch)
        is_latin = 0x0041 <= code <= 0x007A
        is_cyrillic = 0x0400 <= code <= 0x052F
        if not (ch.isdigit() or is_latin or is_cyrillic or ch in allowed_punctuation):
            suspicious += 1
    return suspicious > max(3, len(clean) // 5)


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


async def _delete_invite_codes_for_descriptions(db: AsyncSession, descriptions: set[str]) -> int:
    clean = {item.strip() for item in descriptions if item and item.strip()}
    if not clean:
        return 0

    result = await db.execute(select(InviteCode).where(InviteCode.description.in_(clean)))
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
    for code in codes:
        code.linked_code_id = None
    await db.flush()

    for code in codes:
        await db.delete(code)

    return len(codes)


@router.post("/invite-codes", response_model=List[InviteCodeResponse], dependencies=[Depends(require_admin)])
async def create_invite_codes(payload: InviteCodeCreate, db: AsyncSession = Depends(get_db)):
    role_str = str(payload.role).strip().lower()
    description = _clean_invite_description(payload.description)

    if role_str in ["pair", "student_parent"]:
        child_code = generate_random_code("CHD")
        child_invite = InviteCode(role=RoleEnum.child, code=child_code, description=description)
        db.add(child_invite)
        await db.flush()

        parent_code = generate_random_code("PRN")
        parent_invite = InviteCode(
            role=RoleEnum.parent,
            code=parent_code,
            description=description,
            linked_code_id=child_invite.id
        )
        db.add(parent_invite)

        first_name, last_name = _split_name(description)
        placeholder_password = get_password_hash(secrets.token_urlsafe(24))

        child_user = User(
            email=f"placeholder-child-{child_code.lower()}@pifagor.local",
            hashed_password=placeholder_password,
            first_name=first_name,
            last_name=last_name,
            middle_name="",
            phone=None,
            role=RoleEnum.child,
            is_active=True,
        )
        parent_user = User(
            email=f"placeholder-parent-{parent_code.lower()}@pifagor.local",
            hashed_password=placeholder_password,
            first_name="Родитель",
            last_name=f"{last_name} {first_name}".strip(),
            middle_name="",
            phone=None,
            role=RoleEnum.parent,
            is_active=True,
        )
        db.add_all([child_user, parent_user])
        await db.flush()

        child_profile = ChildProfile(user_id=child_user.id, crm_status="Пробное")
        parent_profile = ParentProfile(user_id=parent_user.id)
        db.add_all([child_profile, parent_profile])
        await db.flush()
        db.add(ParentChild(parent_id=parent_profile.id, child_id=child_profile.id))

        child_invite.used_by_user_id = child_user.id
        parent_invite.used_by_user_id = parent_user.id

        await db.commit()
        return [child_invite, parent_invite]

    if role_str == "tutor":
        code_str = generate_random_code("TUT")
        invite = InviteCode(role=RoleEnum.tutor, code=code_str, description=description)
        db.add(invite)
        await db.commit()
        return [invite]

    raise HTTPException(status_code=400, detail=f"Неверная роль для генерации кода: {payload.role}")


@router.get("/invite-codes", response_model=List[InviteCodeResponse], dependencies=[Depends(require_admin)])
async def list_invite_codes(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(InviteCode).order_by(InviteCode.created_at.desc()))
    codes = result.scalars().all()

    child_users_result = await db.execute(
        select(User.first_name, User.last_name).join(ChildProfile, ChildProfile.user_id == User.id)
    )
    child_user_rows = child_users_result.all()
    active_child_names = {
        f"{last} {first}".strip()
        for first, last in child_user_rows
        if first or last
    } | {
        f"{first} {last}".strip()
        for first, last in child_user_rows
        if first or last
    }

    visible = []
    for code in codes:
        description = (code.description or "").strip()
        if _is_suspicious_invite_description(description):
            continue
        if code.role in (RoleEnum.child, RoleEnum.parent) and description:
            if description not in active_child_names:
                continue
        visible.append(code)
    return visible



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
    """Finance report per student. If week_start is provided, limit it to that week."""
    week_end = week_start + timedelta(days=6) if week_start else None
    lesson_filters = [Lesson.status == LessonStatus.completed]
    receipt_filters = [EmailReceipt.child_id.isnot(None)]

    if week_start and week_end:
        lesson_filters.extend([
            Lesson.date >= week_start,
            Lesson.date <= week_end,
        ])
        receipt_filters.extend([
            cast(EmailReceipt.payment_date, Date) >= week_start,
            cast(EmailReceipt.payment_date, Date) <= week_end,
        ])

    # Completed lessons in the selected range.
    lessons_res = await db.execute(
        select(Lesson.child_id, func.count(Lesson.id).label("cnt"))
        .where(*lesson_filters)
        .group_by(Lesson.child_id)
    )
    lessons_by_child = {row.child_id: row.cnt for row in lessons_res}

    # Paid receipts in the selected range — для учеников с каналом «Прошлогодний»
    # (перенесённых со старой платформы) учитываем только чеки не раньше даты
    # внесения в CRM. Для остальных учеников — все их чеки, без ограничения,
    # так как разница между датой первого занятия и датой оформления в CRM —
    # обычная рабочая задержка, а не перенос старых данных.
    receipts_res = await db.execute(
        select(EmailReceipt.child_id, func.sum(EmailReceipt.amount).label("total"))
        .join(ChildProfile, EmailReceipt.child_id == ChildProfile.id)
        .join(User, ChildProfile.user_id == User.id)
        .where(
            *receipt_filters,
            or_(
                ChildProfile.channel != "Прошлогодний",
                func.coalesce(EmailReceipt.payment_date, EmailReceipt.created_at) >= User.created_at,
            ),
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
        .options(
            joinedload(User.child_profile)
            .joinedload(ChildProfile.parents)
            .joinedload(ParentChild.parent)
            .joinedload(ParentProfile.user)
        )
        .where(User.id == user_id, User.role == RoleEnum.child)
    )
    student = result.scalars().unique().one_or_none()
    if not student or not student.child_profile:
        raise HTTPException(status_code=404, detail="Student not found")

    if payload.lesson_price is not None:
        if payload.lesson_price <= 0:
            raise HTTPException(status_code=400, detail="Lesson price must be greater than zero")
        student.child_profile.lesson_price = payload.lesson_price
    if payload.grade is not None:
        student.child_profile.grade = payload.grade
    if payload.first_name is not None:
        student.first_name = payload.first_name.strip()
    if payload.last_name is not None:
        student.last_name = payload.last_name.strip()
    if payload.phone is not None:
        student.phone = payload.phone.strip() or None
    if payload.status is not None:
        student.child_profile.crm_status = payload.status.strip() or "Пробное"
    if payload.lessons_per_week is not None:
        student.child_profile.lessons_per_week = payload.lessons_per_week
    if payload.notes is not None:
        student.child_profile.notes = payload.notes.strip() or None
    if payload.channel is not None:
        student.child_profile.channel = payload.channel.strip() or None
    if payload.subjects is not None:
        student.child_profile.subjects_text = ", ".join(s.strip() for s in payload.subjects if s.strip()) or None
    if payload.tutors is not None:
        student.child_profile.tutors_text = ", ".join(t.strip() for t in payload.tutors if t.strip()) or None
    if payload.contract_label is not None:
        student.child_profile.contract_label = payload.contract_label.strip() or None

    should_rematch_receipts = any(
        value is not None
        for value in (
            payload.first_name,
            payload.last_name,
            payload.parent_name,
            payload.parent_phone,
        )
    )

    parent_user = None
    for link in student.child_profile.parents:
        if link.parent and link.parent.user:
            parent_user = link.parent.user
            break
    if parent_user:
        if payload.parent_name is not None:
            parts = payload.parent_name.strip().split()
            parent_user.last_name = parts[0] if parts else ""
            parent_user.first_name = " ".join(parts[1:]) if len(parts) > 1 else ""
        if payload.parent_phone is not None:
            parent_user.phone = payload.parent_phone.strip() or None

    await db.commit()
    if should_rematch_receipts:
        from app.services.email_parser import rematch_unlinked_receipts

        await rematch_unlinked_receipts(db)
    return {
        "ok": True,
        "lesson_price": student.child_profile.lesson_price,
        "grade": student.child_profile.grade,
        "first_name": student.first_name,
        "last_name": student.last_name,
        "phone": student.phone,
        "status": student.child_profile.crm_status,
        "lessons_per_week": student.child_profile.lessons_per_week,
        "notes": student.child_profile.notes,
        "channel": student.child_profile.channel,
        "subjects": [s.strip() for s in (student.child_profile.subjects_text or "").split(",") if s.strip()],
        "tutors": [t.strip() for t in (student.child_profile.tutors_text or "").split(",") if t.strip()],
        "contract_label": student.child_profile.contract_label,
        "parent_name": f"{parent_user.last_name} {parent_user.first_name}".strip() if parent_user else "",
        "parent_phone": parent_user.phone if parent_user else "",
    }


@router.get("/students-dashboard", dependencies=[Depends(require_admin)])
async def students_dashboard(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ChildProfile)
        .options(
            joinedload(ChildProfile.user),
            joinedload(ChildProfile.parents).joinedload(ParentChild.parent).joinedload(ParentProfile.user),
            joinedload(ChildProfile.lessons).joinedload(Lesson.subject),
            joinedload(ChildProfile.lessons).joinedload(Lesson.tutor).joinedload(TutorProfile.user),
            joinedload(ChildProfile.lessons).joinedload(Lesson.tutor),
        )
    )
    children = result.scalars().unique().all()
    rows = []
    for child in children:
        user = child.user
        if not user:
            continue
        parents = [link.parent.user for link in child.parents if link.parent and link.parent.user]
        lesson_subjects = sorted({lesson.subject.name for lesson in child.lessons if lesson.subject})
        lesson_tutors = sorted({
            f"{lesson.tutor.user.last_name} {lesson.tutor.user.first_name}".strip()
            for lesson in child.lessons
            if lesson.tutor and lesson.tutor.user
        })
        subjects = [s.strip() for s in (child.subjects_text or "").split(",") if s.strip()] or lesson_subjects
        tutors = [t.strip() for t in (child.tutors_text or "").split(",") if t.strip()] or lesson_tutors
        contract_count_res = await db.execute(
            select(func.count(ParentContract.id)).where(ParentContract.child_id == child.id)
        )
        has_contract = (contract_count_res.scalar() or 0) > 0
        parent_names = [f"{p.last_name} {p.first_name}".strip() for p in parents]
        parent_phones = [p.phone for p in parents if p.phone]
        rows.append({
            "child_id": child.id,
            "user_id": user.id,
            "status": child.crm_status if user.is_active else "Не занимаются",
            "lesson_price": child.lesson_price,
            "student_name": f"{user.last_name} {user.first_name}".strip(),
            "grade": child.grade,
            "lessons_per_week": child.lessons_per_week,
            "subjects": subjects,
            "tutors": tutors,
            "has_contract": has_contract,
            "contract_label": child.contract_label or ("Есть" if has_contract else "Нет"),
            "notes": child.notes or "",
            "student_phone": user.phone,
            "parent_names": parent_names,
            "parent_phones": parent_phones,
            "parent_name": parent_names[0] if parent_names else "",
            "parent_phone": parent_phones[0] if parent_phones else "",
            "channel": child.channel or "",
        })
    return rows


def _serialize_contract(contract: ParentContract) -> dict:
    parent_user = contract.parent.user if contract.parent else None
    child_user = contract.child.user if contract.child else None
    return {
        "id": f"parent-{contract.id}",
        "db_id": contract.id,
        "number": contract.contract_number or f"P-{contract.id:04d}",
        "type": "Родитель",
        "status": "Действующий" if contract.is_signed else "Ожидает подписи",
        "match_status": contract.match_status,
        "needs_review": contract.needs_review,
        "start_date": contract.start_date.isoformat() if contract.start_date else None,
        "end_date": contract.end_date.isoformat() if contract.end_date else None,
        "parent_full_name": contract.parent_full_name or (
            f"{parent_user.last_name} {parent_user.first_name}".strip() if parent_user else ""
        ),
        "parent_phone": contract.parent_phone or (parent_user.phone if parent_user else ""),
        "city": contract.city or "",
        "street": contract.street or "",
        "house": contract.house or "",
        "email": contract.parent_email or (parent_user.email if parent_user else ""),
        "recommendation": contract.recommendation,
        "recommendation_as_of": contract.recommendation_as_of.isoformat() if contract.recommendation_as_of else None,
        "student_name": f"{child_user.last_name} {child_user.first_name}".strip() if child_user else "",
        "child_id": contract.child_id,
        "total_amount": contract.total_amount,
        "file_url": contract.signed_file_url or contract.file_url,
    }


async def _compute_recommendation(
    db: AsyncSession, contract: ParentContract, as_of: date
) -> tuple[Optional[str], Optional[str]]:
    """Сравнивает, сколько занятий должно было пройти согласно графику
    платежей на дату as_of, с тем, сколько реально проведено.
    Возвращает (рекомендация, причина_пропуска) — ровно одно из двух не None."""
    if not contract.child_id:
        return None, "Договор ещё не привязан к ученику"

    if contract.payment_mode == "per_lesson":
        return "После каждого занятия", None
    if contract.payment_mode == "monthly":
        return "Оплата ежемесячно (фиксированного графика по занятиям нет)", None

    payments = json.loads(contract.payments_json) if contract.payments_json else []
    if not payments:
        return None, "Не удалось распознать график платежей в тексте договора — проверьте вручную"

    due_payments = [
        p for p in payments
        if date.fromisoformat(p["due_date"]) <= as_of
    ]
    if not due_payments:
        return None, "На эту дату в договоре ещё нет ни одного срока платежа — договор не исследуется"

    total_due = round(sum(p["amount"] for p in due_payments), 2)

    child_res = await db.execute(select(ChildProfile).where(ChildProfile.id == contract.child_id))
    child = child_res.scalar_one_or_none()
    if not child or not child.lesson_price:
        return None, "У ученика не указана стоимость занятия — расчёт невозможен"

    a = int(total_due // child.lesson_price)

    count_res = await db.execute(
        select(func.count(Lesson.id)).where(
            Lesson.child_id == contract.child_id,
            Lesson.status == LessonStatus.completed,
            Lesson.date <= as_of,
        )
    )
    b = count_res.scalar() or 0

    if a > b:
        return f"Отработать {a - b} занятий (на {as_of.isoformat()})", None
    if b > a:
        return f"Проведено на {b - a} занятий больше (на {as_of.isoformat()})", None
    return f"Соответствует графику оплат (на {as_of.isoformat()})", None


@router.post("/contracts/upload", dependencies=[Depends(require_admin)])
async def upload_contract(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    """Загрузить уже подписанный договор (.docx) — распознаётся автоматически:
    ФИО заказчика, даты, сумма, график платежей, контакты. Договор пытается
    сам привязаться к ученику по совпадению ФИО (та же логика, что и для
    чеков об оплате)."""
    ext = Path(file.filename or "").suffix.lower()
    if ext != ".docx":
        raise HTTPException(status_code=400, detail="Поддерживаются только файлы .docx")

    contents = await file.read()
    if len(contents) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Файл слишком большой, максимум 15 МБ")

    upload_dir = Path(__file__).resolve().parents[4] / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{ext}"
    stored_path = upload_dir / stored_name
    stored_path.write_bytes(contents)

    try:
        parsed = parse_contract_docx(str(stored_path))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Не удалось разобрать договор: {e}")

    child_id = None
    parent_id = None
    match_status = "unmatched"
    match_name = parsed.get("client_name_header") or parsed.get("parent_full_name")
    if match_name:
        child_id = await _find_child_by_payer_name(match_name, db)
        if child_id:
            match_status = "matched"
            parent_link_res = await db.execute(
                select(ParentChild).where(ParentChild.child_id == child_id)
            )
            parent_link = parent_link_res.scalars().first()
            if parent_link:
                parent_id = parent_link.parent_id
        else:
            match_status = "ambiguous_or_unmatched"

    warnings = list(parsed.get("parse_warnings") or [])
    if match_status != "matched":
        warnings.append("Не удалось однозначно привязать договор к ученику — выберите вручную")

    contract = ParentContract(
        parent_id=parent_id,
        child_id=child_id,
        file_url=f"/uploads/{stored_name}",
        is_signed=True,
        contract_number=parsed.get("contract_number"),
        client_name_raw=match_name,
        start_date=parsed.get("start_date"),
        end_date=parsed.get("end_date"),
        total_amount=parsed.get("total_amount"),
        parent_full_name=parsed.get("parent_full_name"),
        parent_phone=parsed.get("parent_phone"),
        parent_email=parsed.get("parent_email"),
        city=parsed.get("city"),
        street=parsed.get("street"),
        house=parsed.get("house"),
        payments_json=json.dumps(parsed.get("payments") or []),
        match_status=match_status,
        needs_review=bool(warnings),
        payment_mode=parsed.get("payment_mode") or "unknown",
    )
    db.add(contract)
    await db.commit()
    await db.refresh(contract)

    result = await db.execute(
        select(ParentContract).options(
            joinedload(ParentContract.parent).joinedload(ParentProfile.user),
            joinedload(ParentContract.child).joinedload(ChildProfile.user),
        ).where(ParentContract.id == contract.id)
    )
    contract = result.unique().scalar_one()
    row = _serialize_contract(contract)
    row["parse_warnings"] = warnings
    return row


class ContractFieldsUpdate(BaseModel):
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    total_amount: Optional[float] = None
    parent_full_name: Optional[str] = None
    parent_phone: Optional[str] = None
    parent_email: Optional[str] = None
    city: Optional[str] = None
    street: Optional[str] = None
    house: Optional[str] = None


@router.patch("/contracts/{contract_id}", dependencies=[Depends(require_admin)])
async def update_contract_fields(
    contract_id: int, payload: ContractFieldsUpdate, db: AsyncSession = Depends(get_db)
):
    """Ручная правка распознанных полей договора (когда парсер ошибся или
    не смог что-то распознать)."""
    result = await db.execute(select(ParentContract).where(ParentContract.id == contract_id))
    contract = result.scalar_one_or_none()
    if not contract:
        raise HTTPException(status_code=404, detail="Договор не найден")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(contract, field, value)
    await db.commit()

    result = await db.execute(
        select(ParentContract).options(
            joinedload(ParentContract.parent).joinedload(ParentProfile.user),
            joinedload(ParentContract.child).joinedload(ChildProfile.user),
        ).where(ParentContract.id == contract_id)
    )
    return _serialize_contract(result.unique().scalar_one())


class ContractAssignChild(BaseModel):
    child_id: int


@router.post("/contracts/{contract_id}/assign-child", dependencies=[Depends(require_admin)])
async def assign_contract_child(
    contract_id: int, payload: ContractAssignChild, db: AsyncSession = Depends(get_db)
):
    """Вручную привязать договор к ученику, если автоматическая привязка не сработала."""
    result = await db.execute(select(ParentContract).where(ParentContract.id == contract_id))
    contract = result.scalar_one_or_none()
    if not contract:
        raise HTTPException(status_code=404, detail="Договор не найден")

    child_res = await db.execute(select(ChildProfile).where(ChildProfile.id == payload.child_id))
    child = child_res.scalar_one_or_none()
    if not child:
        raise HTTPException(status_code=404, detail="Ученик не найден")

    contract.child_id = child.id
    parent_link_res = await db.execute(
        select(ParentChild).where(ParentChild.child_id == child.id)
    )
    parent_link = parent_link_res.scalars().first()
    contract.parent_id = parent_link.parent_id if parent_link else None
    contract.match_status = "matched"
    await db.commit()

    result = await db.execute(
        select(ParentContract).options(
            joinedload(ParentContract.parent).joinedload(ParentProfile.user),
            joinedload(ParentContract.child).joinedload(ChildProfile.user),
        ).where(ParentContract.id == contract_id)
    )
    return _serialize_contract(result.unique().scalar_one())


@router.post("/contracts/{contract_id}/recalculate", dependencies=[Depends(require_admin)])
async def recalculate_contract(
    contract_id: int,
    as_of: Optional[date] = Query(None, description="Дата, на которую считать рекомендацию (по умолчанию — сегодня)"),
    db: AsyncSession = Depends(get_db),
):
    """Пересчитать рекомендацию по договору на указанную дату (для теста —
    можно указать любую прошедшую дату, например ?as_of=2026-08-30)."""
    result = await db.execute(select(ParentContract).where(ParentContract.id == contract_id))
    contract = result.scalar_one_or_none()
    if not contract:
        raise HTTPException(status_code=404, detail="Договор не найден")

    check_date = as_of or date.today()
    recommendation, skip_reason = await _compute_recommendation(db, contract, check_date)
    if recommendation is not None:
        contract.recommendation = recommendation
        contract.recommendation_as_of = check_date
        await db.commit()

    result = await db.execute(
        select(ParentContract).options(
            joinedload(ParentContract.parent).joinedload(ParentProfile.user),
            joinedload(ParentContract.child).joinedload(ChildProfile.user),
        ).where(ParentContract.id == contract_id)
    )
    row = _serialize_contract(result.unique().scalar_one())
    if skip_reason:
        row["skip_reason"] = skip_reason
    return row


async def recalculate_all_contracts(db: AsyncSession, as_of: Optional[date] = None) -> int:
    """Пересчитать рекомендации по всем привязанным к ученику договорам.
    Используется еженедельным фоновым заданием (каждое воскресенье)."""
    check_date = as_of or date.today()
    result = await db.execute(
        select(ParentContract).where(ParentContract.child_id.isnot(None))
    )
    updated = 0
    for contract in result.scalars().all():
        recommendation, _skip_reason = await _compute_recommendation(db, contract, check_date)
        if recommendation is not None:
            contract.recommendation = recommendation
            contract.recommendation_as_of = check_date
            updated += 1
    await db.commit()
    return updated


@router.post("/contracts/recalculate-all", dependencies=[Depends(require_admin)])
async def recalculate_all_contracts_endpoint(
    as_of: Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    updated = await recalculate_all_contracts(db, as_of)
    return {"updated": updated, "as_of": (as_of or date.today()).isoformat()}


@router.get("/contracts-dashboard", dependencies=[Depends(require_admin)])
async def contracts_dashboard(db: AsyncSession = Depends(get_db)):
    parent_result = await db.execute(
        select(ParentContract).options(
            joinedload(ParentContract.parent).joinedload(ParentProfile.user),
            joinedload(ParentContract.child).joinedload(ChildProfile.user),
        )
    )
    return [_serialize_contract(c) for c in parent_result.scalars().unique().all()]


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

def _serialize_tutor(tutor: TutorProfile, earnings: Optional[float] = None) -> TutorProfileOut:
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
        earnings=earnings,
    )


async def _tutor_earnings_map(
    db: AsyncSession,
    tutor_ids: List[int],
    as_of: Optional[date] = None,
) -> dict[int, float]:
    """Сколько должны каждому репетитору: (проведённые занятия * ставка) - уже выплачено.
    Если as_of указан — считаются только занятия с датой не позже as_of (для расчёта
    суммы к оплате при выборе конкретной даты выплаты)."""
    if not tutor_ids:
        return {}

    rates_res = await db.execute(
        select(TutorProfile.id, TutorProfile.rate_per_hour).where(TutorProfile.id.in_(tutor_ids))
    )
    rate_by_tutor = {row[0]: (row[1] or 0) for row in rates_res.all()}

    count_filters = [Lesson.tutor_id.in_(tutor_ids), Lesson.status == LessonStatus.completed]
    if as_of is not None:
        count_filters.append(Lesson.date <= as_of)
    counts_res = await db.execute(
        select(Lesson.tutor_id, func.count(Lesson.id))
        .where(*count_filters)
        .group_by(Lesson.tutor_id)
    )
    count_by_tutor = {row[0]: row[1] for row in counts_res.all()}

    paid_res = await db.execute(
        select(TutorPayout.tutor_id, func.sum(TutorPayout.amount))
        .where(TutorPayout.tutor_id.in_(tutor_ids))
        .group_by(TutorPayout.tutor_id)
    )
    paid_by_tutor = {row[0]: (row[1] or 0) for row in paid_res.all()}

    result = {}
    for tutor_id in tutor_ids:
        total_earned = count_by_tutor.get(tutor_id, 0) * rate_by_tutor.get(tutor_id, 0)
        paid = paid_by_tutor.get(tutor_id, 0)
        result[tutor_id] = round(max(0.0, total_earned - paid), 2)
    return result


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
    earnings_map = await _tutor_earnings_map(db, [t.id for t in tutors])
    return [_serialize_tutor(t, earnings=earnings_map.get(t.id, 0.0)) for t in tutors]


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
    tutor = result.unique().scalar_one()
    earnings_map = await _tutor_earnings_map(db, [tutor.id])
    return _serialize_tutor(tutor, earnings=earnings_map.get(tutor.id, 0.0))


@router.post("/tutors/{tutor_id}/pay", response_model=TutorPayoutOut, dependencies=[Depends(require_admin)])
async def pay_admin_tutor(
    tutor_id: int,
    payload: TutorPayoutCreate,
    db: AsyncSession = Depends(get_db),
):
    """Отметить зарплату репетитору как выплаченную на выбранную дату.
    Оплачивается только то, что заработано занятиями с датой не позже выбранной
    (payload.paid_at) — не весь текущий баланс на сегодня. Остаток за занятия
    после этой даты продолжает числиться как долг."""
    tutor_res = await db.execute(select(TutorProfile).where(TutorProfile.id == tutor_id))
    tutor = tutor_res.scalar_one_or_none()
    if not tutor:
        raise HTTPException(status_code=404, detail=f"Репетитор с ID {tutor_id} не найден")

    earnings_map = await _tutor_earnings_map(db, [tutor_id], as_of=payload.paid_at)
    outstanding = earnings_map.get(tutor_id, 0.0)
    if outstanding <= 0:
        raise HTTPException(status_code=400, detail="На выбранную дату этому репетитору нечего платить")

    payout = TutorPayout(tutor_id=tutor_id, amount=outstanding, paid_at=payload.paid_at)
    db.add(payout)
    if tutor.user_id:
        db.add(Notification(
            user_id=tutor.user_id,
            title="Зарплата выплачена",
            body=f"Вам выплачено {outstanding} BYN за {payload.paid_at}.",
        ))
    await db.commit()
    await db.refresh(payout)

    remaining_map = await _tutor_earnings_map(db, [tutor_id])
    return TutorPayoutOut(
        id=payout.id,
        tutor_id=payout.tutor_id,
        amount=payout.amount,
        paid_at=payout.paid_at,
        created_at=payout.created_at,
        earnings_after=remaining_map.get(tutor_id, 0.0),
    )


@router.delete("/tutors/{tutor_id}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_admin_tutor(tutor_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TutorProfile).where(TutorProfile.id == tutor_id))
    tutor = result.scalar_one_or_none()
    user_id = tutor.user_id if tutor else None
    if not tutor:
        raise HTTPException(status_code=404, detail=f"Репетитор с ID {tutor_id} не найден")

    lessons = await db.execute(select(Lesson).where(Lesson.tutor_id == tutor_id))
    for lesson in lessons.scalars().all():
        hws = await db.execute(select(Homework).where(Homework.lesson_id == lesson.id))
        for hw in hws.scalars().all():
            await db.delete(hw)
        await db.delete(lesson)

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
        (Review, Review.tutor_id),
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

    student_descriptions = {
        f"{user.last_name} {user.first_name}".strip(),
        f"{user.first_name} {user.last_name}".strip(),
    }

    result = await db.execute(select(ChildProfile).where(ChildProfile.user_id == user_id))
    child = result.scalar_one_or_none()
    if not child:
        await _delete_invite_codes_for_user(db, user_id)
        await _delete_invite_codes_for_descriptions(db, student_descriptions)
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
    await _delete_invite_codes_for_descriptions(db, student_descriptions)

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


@router.post("/tutors/{tutor_id}/work-documents", dependencies=[Depends(require_admin)])
async def send_tutor_work_document(
    tutor_id: int,
    payload: TutorWorkDocumentCreate,
    db: AsyncSession = Depends(get_db),
):
    """Админ отправляет репетитору контракт, акт или обычный документ."""
    tutor_res = await db.execute(select(TutorProfile).where(TutorProfile.id == tutor_id))
    if not tutor_res.scalar_one_or_none():
        raise HTTPException(status_code=404, detail=f"Репетитор с ID {tutor_id} не найден")

    kind = payload.kind.strip().lower()
    if kind in {"contract", "контракт", "договор", "договор подряда"}:
        item = TutorContract(tutor_id=tutor_id, file_url=payload.file_url)
        db.add(item)
        stored_kind = "contract"
    elif kind in {"act", "акт"}:
        today = date.today()
        item = Act(
            tutor_id=tutor_id,
            period_start=today.replace(day=1),
            period_end=today,
            lessons_count=0,
            total_amount=0,
            blank_url=payload.file_url,
        )
        db.add(item)
        stored_kind = "act"
    else:
        item = TutorDocument(tutor_id=tutor_id, title=payload.title, file_url=payload.file_url)
        db.add(item)
        stored_kind = "document"

    await db.commit()
    await db.refresh(item)
    return {"ok": True, "kind": stored_kind, "id": item.id}


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

