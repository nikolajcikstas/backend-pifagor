"""Веб-уведомления: подписка браузера администратора, проверка, итоги дня.

Пилот — только администраторы и только важные события:
• новая заявка (с сайта или по рекомендации);
• новый ученик в CRM;
• ученик стал клиентом / отказ или «не занимаются»;
• итоги дня в 21:00 (занятия, выручка, ученики, долг клиентов и как он
  изменился за день).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.deps import require_admin
from app.db.session import get_db
from app.models.models import (
    AppState, ChildProfile, CrmStatusEvent, Lesson, LessonStatus, PushSubscription, RoleEnum, User,
)
from app.services import webpush

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/push", tags=["push"])

# Сервер сам отправляет запрос на адрес подписки — разрешаем только адреса
# настоящих служб доставки (Google/Chrome, Apple/Safari, Mozilla/Firefox, Microsoft/Edge).
PUSH_HOSTS = ("fcm.googleapis.com", "android.googleapis.com", "push.services.mozilla.com",
              "push.apple.com", "notify.windows.com")


def _endpoint_ok(url: str) -> bool:
    try:
        u = urlparse(url)
    except Exception:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)


class Keys(BaseModel):
    p256dh: str = Field(..., min_length=40, max_length=200)
    auth: str = Field(..., min_length=10, max_length=100)


class SubIn(BaseModel):
    endpoint: str = Field(..., min_length=10, max_length=2000)
    keys: Keys
    user_agent: Optional[str] = Field(None, max_length=300)


class UnsubIn(BaseModel):
    endpoint: str = Field(..., min_length=10, max_length=2000)


@router.get("/public-key")
async def public_key(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return {"key": await webpush.vapid_public(db)}


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    n = await db.scalar(select(func.count(PushSubscription.id)).where(PushSubscription.user_id == user.id))
    return {"devices": int(n or 0)}


@router.post("/subscribe")
async def subscribe(body: SubIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    if not _endpoint_ok(body.endpoint):
        raise HTTPException(status_code=400, detail="Неизвестная служба уведомлений")
    try:  # ключ браузера должен быть настоящей точкой P-256
        from cryptography.hazmat.primitives.asymmetric import ec
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), webpush.b64u_dec(body.keys.p256dh))
        if len(webpush.b64u_dec(body.keys.auth)) < 16:
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="Неверные ключи подписки")
    sub = (await db.execute(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))).scalars().first()
    if sub is None:
        sub = PushSubscription(endpoint=body.endpoint)
        db.add(sub)
    sub.user_id, sub.p256dh, sub.auth = user.id, body.keys.p256dh, body.keys.auth
    sub.user_agent, sub.fails = (body.user_agent or "")[:300] or None, 0
    await db.commit()
    return {"ok": True}


@router.post("/unsubscribe")
async def unsubscribe(body: UnsubIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    await db.execute(delete(PushSubscription).where(PushSubscription.endpoint == body.endpoint,
                                                    PushSubscription.user_id == user.id))
    await db.commit()
    return {"ok": True}


@router.post("/test")
async def test(db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    subs = (await db.execute(select(PushSubscription).where(PushSubscription.user_id == user.id))).scalars().all()
    if not subs:
        raise HTTPException(status_code=400, detail="На ваших устройствах уведомления ещё не включены")
    res = await webpush.send_to(db, subs, "Пифагор: проверка",
                                "Уведомления работают. Сюда будут приходить заявки, новые клиенты, отказы и итоги дня.",
                                "/", "test")
    return res


# ─── события CRM: собираем во время сохранения, отправляем после фиксации ─────

LEFT = ("Отказ", "Не занимаются")
CLIENT = "Клиент"


def queue_event(session: Session, kind: str, child_id: int, status: Optional[str] = None) -> None:
    session.info.setdefault("pf_push", []).append((kind, child_id, status))


@event.listens_for(Session, "after_flush")
def _collect_new_children(session, _ctx):
    for obj in session.new:
        if isinstance(obj, ChildProfile) and obj.id:
            queue_event(session, "new", obj.id, obj.crm_status)


@event.listens_for(Session, "after_commit")
def _send_after_commit(session):
    items = session.info.pop("pf_push", None)
    if not items:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    t = loop.create_task(_deliver(items))
    webpush._TASKS.add(t)
    t.add_done_callback(webpush._TASKS.discard)


@event.listens_for(Session, "after_rollback")
def _drop_on_rollback(session):
    session.info.pop("pf_push", None)


async def _deliver(items):
    from app.db.session import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            has = await db.scalar(select(func.count(PushSubscription.id)))
            if not has:
                return
            seen = set()
            for kind, cid, st in items:
                if (kind, cid, st) in seen:
                    continue
                seen.add((kind, cid, st))
                name = (await db.execute(
                    select(User.last_name, User.first_name).join(ChildProfile, ChildProfile.user_id == User.id)
                    .where(ChildProfile.id == cid)
                )).first()
                who = f"{name[0] or ''} {name[1] or ''}".strip() if name else f"ученик #{cid}"
                if kind == "new":
                    await webpush.push_admins("Новый ученик в CRM", f"{who} · статус: {st or '—'}", "/", f"crm-new-{cid}")
                elif kind == "status" and st == CLIENT:
                    await webpush.push_admins("Новый клиент", f"{who} стал клиентом", "/", f"crm-st-{cid}")
                elif kind == "status" and st in LEFT:
                    await webpush.push_admins("Отказ ученика" if st == "Отказ" else "Ученик не занимается",
                                              f"{who} · статус: {st}", "/", f"crm-st-{cid}")
    except Exception:
        logger.exception("CRM push failed")


# ─── итоги дня в 21:00 по Минску ─────────────────────────────────────────────

DIGEST_HOUR = 21


async def _state_get(db, key: str) -> Optional[str]:
    row = await db.get(AppState, key)
    return row.value if row else None


async def _state_set(db, key: str, value: str) -> None:
    row = await db.get(AppState, key)
    if row is None:
        db.add(AppState(key=key, value=value, updated_at=datetime.utcnow()))
    else:
        row.value, row.updated_at = value, datetime.utcnow()


def _money(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ") + " р"


async def daily_digest(db, day) -> Optional[tuple[str, str]]:
    from app.services.finance_report import compute_finance_rows
    from app.services.pricing import load_prices
    rows = (await db.execute(
        select(Lesson.child_id, Lesson.date).where(Lesson.status == LessonStatus.completed, Lesson.date == day)
    )).all()
    prices = await load_prices(db, [r[0] for r in rows])
    revenue = sum(prices.price(c, d) for c, d in rows)
    start, end = datetime.combine(day, datetime.min.time()) - timedelta(hours=3), \
        datetime.combine(day, datetime.min.time()) + timedelta(hours=21)
    ev = (await db.execute(
        select(CrmStatusEvent.new_status, func.count()).where(CrmStatusEvent.changed_at >= start,
                                                              CrmStatusEvent.changed_at < end)
        .group_by(CrmStatusEvent.new_status)
    )).all()
    ev = {k: v for k, v in ev}
    new_children = await db.scalar(
        select(func.count(ChildProfile.id)).join(User, User.id == ChildProfile.user_id)
        .where(User.created_at >= start, User.created_at < end)
    ) or 0
    clients = await db.scalar(
        select(func.count(ChildProfile.id)).join(User, User.id == ChildProfile.user_id)
        .where(ChildProfile.crm_status == CLIENT, User.is_active.isnot(False))
    ) or 0
    debt = sum(max(0.0, float(r.debt or 0)) for r in await compute_finance_rows(db))
    prev = json.loads(await _state_get(db, "digest_prev") or "{}")
    d_debt = debt - prev["debt"] if "debt" in prev else None
    d_cl = clients - prev["clients"] if "clients" in prev else None
    await _state_set(db, "digest_prev", json.dumps({"debt": round(debt, 2), "clients": clients, "day": day.isoformat()}))
    sign = lambda x: ("+" if x > 0 else "−" if x < 0 else "±") + (_money(abs(x)) if isinstance(x, float) else str(abs(x)))
    lines = [f"Проведено занятий: {len(rows)} на {_money(revenue)}"]
    lines.append(f"Клиентов: {clients}" + (f" ({sign(d_cl)} за день)" if d_cl else ""))
    moves = []
    if new_children:
        moves.append(f"новых в CRM {new_children}")
    if ev.get(CLIENT):
        moves.append(f"стали клиентами {ev[CLIENT]}")
    left = sum(ev.get(s, 0) for s in LEFT)
    if left:
        moves.append(f"отказов {left}")
    if moves:
        t = ", ".join(moves)
        lines.append(t[:1].upper() + t[1:])
    lines.append(f"Клиенты должны: {_money(debt)}" + (f" ({sign(round(d_debt, 2))} за день)" if d_debt and abs(d_debt) >= 1 else ""))
    return f"Итоги дня {day.strftime('%d.%m')}", "\n".join(lines)


async def digest_task():
    """Раз в 5 минут: после 21:00 по Минску — один раз за день итоги дня."""
    from app.db.session import AsyncSessionLocal
    await asyncio.sleep(60)
    while True:
        try:
            now = datetime.utcnow() + timedelta(hours=3)
            if now.hour >= DIGEST_HOUR:
                async with AsyncSessionLocal() as db:
                    has = await db.scalar(select(func.count(PushSubscription.id)))
                    if has and await _state_get(db, "digest_sent") != now.date().isoformat():
                        await _state_set(db, "digest_sent", now.date().isoformat())
                        await db.commit()
                        msg = await daily_digest(db, now.date())
                        await db.commit()
                        if msg:
                            await webpush.push_admins(msg[0], msg[1], "/", "digest")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("daily digest failed")
        await asyncio.sleep(300)
