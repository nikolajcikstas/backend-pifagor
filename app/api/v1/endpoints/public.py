from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.session import get_db
from app.models.models import Subject, Price, Review, FAQ, LeadRequest, User
from app.schemas.schemas import (
    SubjectOut, SubjectCreate,
    PriceOut, PriceCreate,
    ReviewOut, ReviewCreate,
    FAQOut, FAQCreate,
    LeadRequestCreate, LeadRequestOut,
)
from app.core.deps import get_current_user, require_admin
from app.services.email_notification import send_lead_notification

router = APIRouter(tags=["public"])


# ─── Subjects ─────────────────────────────────────────────────────────────────

@router.get("/subjects", response_model=List[SubjectOut])
async def list_subjects(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Subject).where(Subject.is_active == True))
    return result.scalars().all()


@router.get("/subjects/{slug}", response_model=SubjectOut)
async def get_subject(slug: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Subject).where(Subject.slug == slug))
    subject = result.scalar_one_or_none()
    if not subject:
        raise HTTPException(status_code=404, detail="Subject not found")
    return subject


@router.post("/subjects", response_model=SubjectOut, status_code=201)
async def create_subject(
    data: SubjectCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    subject = Subject(**data.model_dump())
    db.add(subject)
    await db.commit()
    await db.refresh(subject)
    return subject


# ─── Prices ───────────────────────────────────────────────────────────────────

@router.get("/prices", response_model=List[PriceOut])
async def list_prices(
    subject_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    q = select(Price).where(Price.is_active == True)
    if subject_id:
        q = q.where(Price.subject_id == subject_id)
    result = await db.execute(q)
    return result.scalars().all()


@router.post("/prices", response_model=PriceOut, status_code=201)
async def create_price(
    data: PriceCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    price = Price(**data.model_dump())
    db.add(price)
    await db.commit()
    await db.refresh(price)
    return price


# ─── Reviews ──────────────────────────────────────────────────────────────────

@router.get("/reviews", response_model=List[ReviewOut])
async def list_reviews(
    tutor_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    q = select(Review).where(Review.is_published == True)
    if tutor_id:
        q = q.where(Review.tutor_id == tutor_id)
    result = await db.execute(q)
    return result.scalars().all()


@router.post("/reviews", response_model=ReviewOut, status_code=201)
async def create_review(data: ReviewCreate, db: AsyncSession = Depends(get_db)):
    review = Review(**data.model_dump())
    db.add(review)
    await db.commit()
    await db.refresh(review)
    return review


# ─── FAQ ──────────────────────────────────────────────────────────────────────

@router.get("/faq", response_model=List[FAQOut])
async def list_faq(
    subject_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    q = select(FAQ).where(FAQ.is_active == True).order_by(FAQ.order)
    if subject_id:
        q = q.where(FAQ.subject_id == subject_id)
    result = await db.execute(q)
    return result.scalars().all()


@router.post("/faq", response_model=FAQOut, status_code=201)
async def create_faq(
    data: FAQCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    faq = FAQ(**data.model_dump())
    db.add(faq)
    await db.commit()
    await db.refresh(faq)
    return faq


# ─── Lead Requests (free lesson form) ────────────────────────────────────────

@router.post("/requests", response_model=LeadRequestOut, status_code=201)
async def create_lead_request(data: LeadRequestCreate, db: AsyncSession = Depends(get_db)):
    req = LeadRequest(**data.model_dump())
    db.add(req)
    await db.commit()
    await db.refresh(req)

    subject_name = ""
    if data.subject_id:
        result = await db.execute(select(Subject).where(Subject.id == data.subject_id))
        subject = result.scalar_one_or_none()
        if subject:
            subject_name = subject.name

    send_lead_notification(
        name=data.name,
        phone=data.phone,
        subject_name=subject_name,
        message=data.message or "",
    )

    return req


@router.get("/requests", response_model=List[LeadRequestOut])
async def list_lead_requests(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(LeadRequest).order_by(LeadRequest.created_at.desc()))
    return result.scalars().all()


@router.patch("/requests/{request_id}/status")
async def update_request_status(
    request_id: int,
    status: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(LeadRequest).where(LeadRequest.id == request_id))
    req = result.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    req.status = status
    await db.commit()
    return {"ok": True}
