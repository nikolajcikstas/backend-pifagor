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
    TutorPayout, ParentChild, EmailReceipt, ParentProfile,
)
from app.schemas.schemas import (
    ReportCreate, ReportOut,
    HomeworkCreate, HomeworkOut,
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

@router.get("/reports", response_model=List[ReportOut])
async def list_reports(
    child_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(Report)
    if current_user.role == RoleEnum.tutor and current_user.tutor_profile:
        q = q.where(Report.tutor_id == current_user.tutor_profile.id)
    elif current_user.role == RoleEnum.child and current_user.child_profile:
        q = q.where(Report.child_id == current_user.child_profile.id)
    elif current_user.role == RoleEnum.parent and current_user.parent_profile:
        child_ids = [pc.child_id for pc in current_user.parent_profile.children]
        if child_id and child_id not in child_ids:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        q = q.where(Report.child_id == (child_id or child_ids[0]) if child_id else Report.child_id.in_(child_ids))
    elif current_user.role == RoleEnum.admin:
        if child_id:
            q = q.where(Report.child_id == child_id)
    else:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    result = await db.execute(q.order_by(Report.created_at.desc()))
    return result.scalars().all()


@router.post("/reports", response_model=ReportOut, status_code=201)
async def create_report(
    data: ReportCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_tutor),
):
    report = Report(**data.model_dump(), tutor_id=current_user.tutor_profile.id)
    db.add(report)
    child = await db.scalar(select(ChildProfile).where(ChildProfile.id == data.child_id).options(selectinload(ChildProfile.user)))
    await _notify_admins(
        db,
        "Новый отчёт",
        f"Репетитор {_user_name(current_user)} добавил отчёт по ученику {_user_name(child.user if child else None)}.",
    )
    await db.commit()
    await db.refresh(report)
    return report


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

@router.get("/homeworks", response_model=List[HomeworkOut])
async def list_homeworks(
    child_id: Optional[int] = Query(None),
    lesson_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(Homework)
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
    result = await db.execute(q)
    return result.scalars().all()


@router.post("/homeworks", response_model=HomeworkOut, status_code=201)
async def create_homework(
    data: HomeworkCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_tutor),
):
    lesson = await db.scalar(
        select(Lesson)
        .where(Lesson.id == data.lesson_id)
        .options(selectinload(Lesson.child).selectinload(ChildProfile.user))
    )
    if not lesson or lesson.tutor_id != current_user.tutor_profile.id or lesson.child_id != data.child_id:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    hw = Homework(**data.model_dump())
    db.add(hw)
    await _notify(
        db,
        lesson.child.user_id if lesson.child else None,
        "Новое домашнее задание",
        f"Репетитор {_user_name(current_user)} добавил ДЗ по занятию {lesson.date}.",
    )
    await _notify_admins(
        db,
        "Домашнее задание добавлено",
        f"Репетитор {_user_name(current_user)} добавил ДЗ ученику {_user_name(lesson.child.user if lesson.child else None)}.",
    )
    await db.commit()
    await db.refresh(hw)
    return hw


class HomeworkSubmitBody(BaseModel):
    submission_url: Optional[str] = None


@router.patch("/homeworks/{hw_id}/submit")
async def submit_homework(
    hw_id: int,
    body: Optional[HomeworkSubmitBody] = Body(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Homework).where(Homework.id == hw_id))
    hw = result.scalar_one_or_none()
    if not hw:
        raise HTTPException(status_code=404, detail="Homework not found")
    if current_user.role == RoleEnum.child and current_user.child_profile:
        if hw.child_id != current_user.child_profile.id:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
    elif current_user.role != RoleEnum.admin:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    if body and body.submission_url:
        hw.submission_url = body.submission_url
    hw.is_done = True
    lesson = await db.scalar(select(Lesson).where(Lesson.id == hw.lesson_id).options(selectinload(Lesson.tutor).selectinload(TutorProfile.user)))
    await _notify(
        db,
        lesson.tutor.user_id if lesson and lesson.tutor else None,
        "ДЗ выполнено",
        f"Ученик {_user_name(current_user)} отправил домашнее задание.",
    )
    await _notify_admins(db, "ДЗ выполнено", f"Ученик {_user_name(current_user)} отправил домашнее задание.")
    await db.commit()
    return {"ok": True}


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
    elif child_id:
        q = q.where(Payment.child_id == child_id)
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
    _: User = Depends(get_current_user),
):
    result_obj = TestResult(test_id=test_id, child_id=data.child_id, answers_json=data.answers_json)
    db.add(result_obj)
    await db.commit()
    await db.refresh(result_obj)
    return result_obj


@router.get("/tests/{test_id}/results", response_model=List[TestResultOut])
async def get_test_results(
    test_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(TestResult).where(TestResult.test_id == test_id))
    return result.scalars().all()


# ─── Notifications ────────────────────────────────────────────────────────────

@router.get("/notifications", response_model=List[NotificationOut])
async def list_notifications(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.models.models import Notification
    result = await db.execute(
        select(Notification)
        .where(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
    )
    return result.scalars().all()


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
