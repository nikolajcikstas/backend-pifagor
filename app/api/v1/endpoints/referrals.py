"""Реферальные ссылки: публичная страница pifagor.by/r/<код> и управление в CRM.
Правила подсчёта — в app/services/referrals.py."""
import asyncio
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_admin
from app.db.session import get_db
from app.models.models import ChildProfile, CrmStatusEvent, LeadRequest, ParentChild, ParentProfile, ReferralLink, RoleEnum, StudentPriceHistory, Subject, User
from app.services import referrals as R
from app.services.pricing import BASE_PRICE, discount_offer, discount_state, minsk_today, set_price_from

router = APIRouter(tags=["referrals"])


async def _child_by_code(db: AsyncSession, code: str) -> Optional[ChildProfile]:
    code = (code or "").strip().upper()
    if not code or len(code) > 12:
        return None
    return (await db.execute(select(ChildProfile).where(ChildProfile.ref_code == code))).scalars().first()


async def _name(db: AsyncSession, child_id: int) -> str:
    u = (await db.execute(
        select(User).join(ChildProfile, ChildProfile.user_id == User.id).where(ChildProfile.id == child_id)
    )).scalars().first()
    return R.person_name(u)


async def _parent_name(db: AsyncSession, child_id: int) -> str:
    """Имя родителя ученика (приглашают обычно родители); если родителя нет — пусто."""
    rows = (await db.execute(
        select(User.last_name, User.first_name)
        .join(ParentProfile, ParentProfile.user_id == User.id)
        .join(ParentChild, ParentChild.parent_id == ParentProfile.id)
        .where(ParentChild.child_id == child_id)
        .order_by(ParentChild.id)
    )).all()
    for ln, fn in rows:
        name = f"{(ln or '').strip()} {(fn or '').strip()}".strip()
        if name:
            return name
    return ""


# ─── публичная страница ───────────────────────────────────────────────────────

@router.get("/ref/{code}")
async def ref_info(code: str, db: AsyncSession = Depends(get_db)):
    child = await _child_by_code(db, code)
    if not child:
        raise HTTPException(status_code=404, detail="Ссылка не найдена")
    subjects = (await db.execute(select(Subject.id, Subject.name).where(Subject.is_active == True).order_by(Subject.name))).all()
    return {"code": child.ref_code, "referrer_name": await _parent_name(db, child.id) or await _name(db, child.id),
            "subjects": [{"id": i, "name": n} for i, n in subjects]}


class RefLeadIn(BaseModel):
    name: str
    phone: str
    child_name: Optional[str] = None
    grade: Optional[str] = None
    subject_id: Optional[int] = None
    message: Optional[str] = None
    consent: bool = False    # не обязательно: форма та же, что на главной сайта
    staff: bool = False      # форму отправили из браузера, где открыта CRM — тестовая заявка
    website: Optional[str] = None  # скрытое поле от ботов


@router.post("/ref/{code}/lead", status_code=201)
async def ref_lead(code: str, body: RefLeadIn, db: AsyncSession = Depends(get_db)):
    child = await _child_by_code(db, code)
    if not child:
        raise HTTPException(status_code=404, detail="Ссылка не найдена")
    if body.website:
        return {"ok": True}  # бот заполнил скрытое поле — молча игнорируем
    name, phone = (body.name or "").strip(), (body.phone or "").strip()
    if len(name) < 2 or len(name) > 200:
        raise HTTPException(status_code=400, detail="Укажите имя")
    phone_n = R.norm_phone(phone)
    if not phone_n or len(phone) > 30:
        raise HTTPException(status_code=400, detail="Укажите телефон полностью, например +375 29 123-45-67")
    referrer = await _name(db, child.id)
    referrer_parent = await _parent_name(db, child.id)
    flag = await R.lead_flag(db, child.id, phone_n, body.staff)
    subject_name = ""
    if body.subject_id:
        subject_name = await db.scalar(select(Subject.name).where(Subject.id == body.subject_id)) or ""
    parts = [f"По рекомендации: {referrer_parent}, родитель ученика {referrer} (код {child.ref_code})" if referrer_parent
             else f"По рекомендации: {referrer} (код {child.ref_code})"]
    if body.child_name and body.child_name.strip():
        parts.append(f"Ребёнок: {body.child_name.strip()[:200]}")
    if body.grade and body.grade.strip():
        parts.append(f"Класс: {body.grade.strip()[:20]}")
    if body.message and body.message.strip():
        parts.append(f"Комментарий: {body.message.strip()[:1000]}")
    if flag:
        parts.append(f"Не засчитана: {R.FLAG_LABELS.get(flag, flag)}")
    lead = LeadRequest(
        name=name, phone=phone, subject_id=body.subject_id if subject_name else None, message="\n".join(parts),
        ref_code=child.ref_code, referrer_child_id=child.id, ref_flag=flag,
        child_name=(body.child_name or "").strip()[:200] or None, grade=(body.grade or "").strip()[:20] or None,
        phone_norm=phone_n,
    )
    db.add(lead)
    await db.commit()
    try:
        from app.services.email_notification import send_lead_notification
        await asyncio.to_thread(send_lead_notification, name=name, phone=phone,
                                subject_name=subject_name, message=lead.message)
    except Exception:
        pass
    return {"ok": True}


# ─── CRM ──────────────────────────────────────────────────────────────────────

@router.get("/referrals/child/{child_id}", dependencies=[Depends(require_admin)])
async def referral_details(child_id: int, db: AsyncSession = Depends(get_db)):
    """Кого привёл клиент: заявки и приглашённые ученики."""
    child = await db.get(ChildProfile, child_id)
    if not child:
        raise HTTPException(status_code=404, detail="Ученик не найден")
    leads = (await db.execute(
        select(LeadRequest).where(LeadRequest.referrer_child_id == child_id).order_by(LeadRequest.created_at.desc())
    )).scalars().all()
    links = (await db.execute(select(ReferralLink).where(ReferralLink.referrer_child_id == child_id))).scalars().all()
    clients = await R.ever_clients(db, [l.child_id for l in links])
    invited = []
    for l in links:
        u = (await db.execute(
            select(User, ChildProfile.crm_status).join(ChildProfile, ChildProfile.user_id == User.id)
            .where(ChildProfile.id == l.child_id)
        )).first()
        invited.append({"child_id": l.child_id, "name": R.person_name(u[0]) if u else "", "status": u[1] if u else None,
                        "is_client": l.child_id in clients, "source": l.source,
                        "created_at": l.created_at.isoformat() + "Z" if l.created_at else None})
    return {
        "child_id": child_id, "name": await _name(db, child_id), "code": child.ref_code,
        "leads": [{"id": x.id, "name": x.name, "phone": x.phone, "child_name": x.child_name,
                   "created_at": x.created_at.isoformat() + "Z" if x.created_at else None,
                   "counted": x.ref_flag is None, "flag": x.ref_flag,
                   "flag_label": R.FLAG_LABELS.get(x.ref_flag) if x.ref_flag else None} for x in leads],
        "invited": invited,
    }


class SentIn(BaseModel):
    sent: bool


@router.patch("/referrals/child/{child_id}/sent", dependencies=[Depends(require_admin)])
async def set_sent(child_id: int, body: SentIn, db: AsyncSession = Depends(get_db)):
    child = await db.get(ChildProfile, child_id)
    if not child:
        raise HTTPException(status_code=404, detail="Ученик не найден")
    child.ref_sent_at = datetime.utcnow() if body.sent else None
    await db.commit()
    return {"ref_sent_at": child.ref_sent_at.isoformat() + "Z" if child.ref_sent_at else None}


@router.post("/referrals/child/{child_id}/discount", dependencies=[Depends(require_admin)])
async def apply_discount(child_id: int, db: AsyncSession = Depends(get_db)):
    """Применить положенную скидку за рекомендации с сегодняшнего дня.
    Прошлые занятия остаются по прежней цене (история цен)."""
    child = await db.get(ChildProfile, child_id)
    if not child:
        raise HTTPException(status_code=404, detail="Ученик не найден")
    stats = (await R.referral_stats(db)).get(child_id, {})
    st = (await discount_state(db, [child_id])).get(child_id, {"applied": 0, "base": None})
    offer = discount_offer(child.lesson_price, st["applied"], st["base"], stats.get("leads", 0))
    if not offer:
        raise HTTPException(status_code=400, detail="Скидка сейчас не положена")
    await set_price_from(db, child, offer["to"], minsk_today(), discount_pct=offer["pct"],
                         reason=f"Скидка {offer['pct']}% за рекомендации")
    await db.commit()
    return {"lesson_price": child.lesson_price, "discount_pct": offer["pct"]}


class CountIn(BaseModel):
    counted: bool


@router.patch("/referrals/leads/{lead_id}/count", dependencies=[Depends(require_admin)])
async def set_lead_counted(lead_id: int, body: CountIn, db: AsyncSession = Depends(get_db)):
    """«Не считать» / «Считать» реферальную заявку."""
    lead = await db.get(LeadRequest, lead_id)
    if not lead or not lead.referrer_child_id:
        raise HTTPException(status_code=404, detail="Реферальная заявка не найдена")
    if body.counted:
        lead.ref_flag = None
    else:
        lead.ref_flag = "excluded"
    await db.commit()
    return {"counted": lead.ref_flag is None, "flag": lead.ref_flag}


# ─── аналитика ────────────────────────────────────────────────────────────────

@router.get("/referrals/analytics", dependencies=[Depends(require_admin)])
async def referral_analytics(start: date = Query(...), end: date = Query(...), db: AsyncSession = Depends(get_db)):
    """За период: ссылок выслано, заявок (засчитанных), пришли клиентами, топ рекомендателей.
    «Пришёл» — дата, когда приглашённый стал клиентом (или когда его связали
    с рекомендателем, если он уже был клиентом)."""
    a = datetime.combine(start, datetime.min.time()) - timedelta(hours=3)
    b = datetime.combine(end + timedelta(days=1), datetime.min.time()) - timedelta(hours=3)
    sent = await db.scalar(select(func.count(ChildProfile.id)).where(ChildProfile.ref_sent_at >= a, ChildProfile.ref_sent_at < b)) or 0
    leads = (await db.execute(
        select(LeadRequest.referrer_child_id, LeadRequest.ref_flag)
        .where(LeadRequest.referrer_child_id.isnot(None), LeadRequest.created_at >= a, LeadRequest.created_at < b)
    )).all()
    per: dict[int, dict] = {}
    for rid, flag in leads:
        s = per.setdefault(rid, {"leads": 0, "clients": 0})
        if flag is None:
            s["leads"] += 1
    links = (await db.execute(select(ReferralLink))).scalars().all()
    ids = [l.child_id for l in links]
    first_client = dict((await db.execute(
        select(CrmStatusEvent.child_id, func.min(CrmStatusEvent.changed_at))
        .where(CrmStatusEvent.new_status == R.CLIENT, CrmStatusEvent.child_id.in_(ids or [0]))
        .group_by(CrmStatusEvent.child_id)
    )).all())
    clients_now = await R.ever_clients(db, ids)
    came = 0
    for l in links:
        if l.child_id not in clients_now:
            continue
        when = max(l.created_at, first_client.get(l.child_id) or l.created_at)
        if a <= when < b:
            came += 1
            per.setdefault(l.referrer_child_id, {"leads": 0, "clients": 0})["clients"] += 1
    counted = sum(s["leads"] for s in per.values())
    top = []
    for rid, s in per.items():
        if s["leads"] or s["clients"]:
            top.append({"child_id": rid, "name": await _name(db, rid), **s})
    top.sort(key=lambda x: (-x["clients"], -x["leads"], x["name"]))
    total_codes = await db.scalar(select(func.count(ChildProfile.id)).where(ChildProfile.ref_code.isnot(None))) or 0
    sent_total = await db.scalar(select(func.count(ChildProfile.id)).where(ChildProfile.ref_sent_at.isnot(None))) or 0
    return {
        "sent": sent, "leads": counted, "leads_all": len(leads), "came": came,
        "conversion_pct": round(100 * came / counted, 1) if counted else None,
        "top": top[:10], "codes_total": total_codes, "sent_total": sent_total,
    }


# ─── личный кабинет родителя ──────────────────────────────────────────────────

@router.get("/cabinet/referral")
async def my_referral(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Для родителя: реферальная ссылка каждого ребёнка-клиента, сколько заявок
    по ней пришло и скидка (действующая или положенная) с обоснованием."""
    if user.role != RoleEnum.parent or not user.parent_profile:
        return {"items": []}
    ids = list((await db.execute(
        select(ParentChild.child_id).where(ParentChild.parent_id == user.parent_profile.id)
    )).scalars().all())
    if not ids:
        return {"items": []}
    children = (await db.execute(select(ChildProfile).where(ChildProfile.id.in_(ids)))).scalars().all()
    stats = await R.referral_stats(db)
    disc = await discount_state(db, ids)
    today = minsk_today()
    items = []
    for ch in children:
        if not ch.ref_code:
            continue
        st = stats.get(ch.id, {})
        ds = disc.get(ch.id, {"applied": 0, "base": None})
        leads = st.get("leads", 0)
        applied = None
        if ds["applied"]:
            since = await db.scalar(
                select(StudentPriceHistory.effective_from)
                .where(StudentPriceHistory.child_id == ch.id, StudentPriceHistory.discount_pct == ds["applied"],
                       StudentPriceHistory.effective_from <= today)
                .order_by(StudentPriceHistory.effective_from.desc()).limit(1))
            applied = {"pct": ds["applied"], "price": ch.lesson_price, "base_price": ds["base"] or BASE_PRICE,
                       "since": since.isoformat() if since else None}
        offer = discount_offer(ch.lesson_price, ds["applied"], ds["base"], leads)
        base = ds["base"] if (ds["applied"] and ds["base"]) else ch.lesson_price
        items.append({
            "child_id": ch.id, "child_name": await _name(db, ch.id), "code": ch.ref_code,
            "url": f"https://pifagor.by/r/{ch.ref_code}",
            "leads": leads, "clients": st.get("clients", 0),
            "discount": applied, "offer": offer,
            "program": abs((base or 0) - BASE_PRICE) < 0.001,  # скидки действуют при цене 40 BYN
        })
    return {"items": items}
