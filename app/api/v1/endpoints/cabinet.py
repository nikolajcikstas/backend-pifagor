import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Body, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.db.session import get_db
from app.models.models import (
    Report, Homework, Payment, Test, TestQuestion, TestAnswer,
    TestResult, Notification, Comment, Act, ParentContract, TutorContract,
    User, RoleEnum, Lesson, LessonStatus, TutorProfile, TutorDocument
)
from app.schemas.schemas import (
    ReportCreate, ReportOut,
    HomeworkCreate, HomeworkOut,
    PaymentCreate, PaymentOut,
    TestCreate, TestOut, TestResultCreate, TestResultOut,
    NotificationOut, CommentCreate, CommentOut,
    ActOut, ParentContractOut, TutorContractOut,
    TutorDocumentOut,
)
from app.core.deps import get_current_user, require_admin, require_tutor

router = APIRouter(tags=["cabinet"])


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
    elif child_id:
        q = q.where(Report.child_id == child_id)
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
    elif child_id:
        q = q.where(Homework.child_id == child_id)
    if lesson_id:
        q = q.where(Homework.lesson_id == lesson_id)
    result = await db.execute(q)
    return result.scalars().all()


@router.post("/homeworks", response_model=HomeworkOut, status_code=201)
async def create_homework(
    data: HomeworkCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_tutor),
):
    hw = Homework(**data.model_dump())
    db.add(hw)
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
    if body and body.submission_url:
        hw.submission_url = body.submission_url
    hw.is_done = True
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


# ─── Comments ─────────────────────────────────────────────────────────────────

@router.post("/comments", response_model=CommentOut, status_code=201)
async def create_comment(
    data: CommentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != RoleEnum.parent or not current_user.parent_profile:
        raise HTTPException(status_code=403, detail="Only parents can comment")
    comment = Comment(**data.model_dump(), parent_id=current_user.parent_profile.id)
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    return comment


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


@router.patch("/contracts/parent/{contract_id}/sign")
async def sign_parent_contract(
    contract_id: int,
    signed_file_url: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(ParentContract).where(ParentContract.id == contract_id))
    contract = result.scalar_one_or_none()
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")
    contract.signed_file_url = signed_file_url
    contract.is_signed = True
    await db.commit()
    return {"ok": True}


@router.post("/parent/contract/{contract_id}/signed")
async def upload_parent_signed_contract(
    contract_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ext = Path(file.filename or "").suffix.lower()
    if ext != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are allowed")

    result = await db.execute(select(ParentContract).where(ParentContract.id == contract_id))
    contract = result.scalar_one_or_none()
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")
    if current_user.role == RoleEnum.parent and current_user.parent_profile:
        if contract.parent_id != current_user.parent_profile.id:
            raise HTTPException(status_code=403, detail="Forbidden")

    contents = await file.read()
    if len(contents) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File is too large")

    upload_dir = Path(__file__).resolve().parents[3] / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{ext}"
    (upload_dir / stored_name).write_bytes(contents)
    file_url = f"/uploads/{stored_name}"

    contract.signed_file_url = file_url
    contract.is_signed = True
    await db.commit()
    return {"ok": True, "file_url": file_url}


@router.get("/parent/contract/{contract_id}/file")
async def download_parent_contract(
    contract_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(ParentContract).where(ParentContract.id == contract_id))
    contract = result.scalar_one_or_none()
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")
    if current_user.role == RoleEnum.parent and current_user.parent_profile:
        if contract.parent_id != current_user.parent_profile.id:
            raise HTTPException(status_code=403, detail="Forbidden")

    file_url = contract.signed_file_url or contract.file_url
    if not file_url:
        raise HTTPException(status_code=404, detail="Contract file not found")
    if file_url.startswith("/uploads/"):
        file_path = Path(__file__).resolve().parents[3] / file_url.lstrip("/")
        if file_path.exists():
            return FileResponse(file_path, media_type="application/pdf", filename=file_path.name)
    return RedirectResponse(file_url)


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


# ─── GET Comments (for parent) ────────────────────────────────────────────────

@router.get("/comments", response_model=List[CommentOut])
async def list_comments(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(Comment)
    if current_user.role == RoleEnum.parent and current_user.parent_profile:
        q = q.where(Comment.parent_id == current_user.parent_profile.id)
    elif current_user.role == RoleEnum.tutor and current_user.tutor_profile:
        q = q.where(Comment.tutor_id == current_user.tutor_profile.id)
    result = await db.execute(q.order_by(Comment.created_at.desc()))
    return result.scalars().all()


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

    # Count completed lessons
    done_result = await db.execute(
        select(func.count(Lesson.id))
        .where(Lesson.tutor_id == tutor_id, Lesson.status == LessonStatus.completed)
    )
    lessons_done = done_result.scalar() or 0

    # Count acts
    acts_result = await db.execute(
        select(func.count(Act.id)).where(Act.tutor_id == tutor_id)
    )
    acts_count = acts_result.scalar() or 0

    # Estimate earnings (rate_per_hour from profile)
    rate = current_user.tutor_profile.rate_per_hour or 0
    earnings = round(lessons_done * rate, 2) if rate else lessons_done * 80

    return {
        "lessons_done": lessons_done,
        "earnings": earnings,
        "acts_count": acts_count,
    }
