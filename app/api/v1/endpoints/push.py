"""Веб-уведомления: подписка браузера и проверка.

Администраторы — важные события; уведомление уходит сразу, как только
событие произошло:
• новая заявка (с сайта или по рекомендации);
• новый ученик в CRM;
• ученик стал клиентом / отказ или «не занимаются».
Родители — напоминание о задолженности больше 160 р (app/services/debt_reminders.py).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.deps import require_parent
from app.db.session import get_db
from app.models.models import ChildProfile, PushSubscription, RoleEnum, User
from app.services import webpush

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/push", tags=["push"])

# Сервер сам отправляет запрос на адрес подписки — разрешаем только публичные
# адреса https (не IP, не localhost и не внутренние имена). Подписываться
# может только администратор. Служба доставки у каждого браузера своя
# (Google — Chrome, Microsoft — Edge, Mozilla — Firefox, Apple — Safari,
# Яндекс, Opera и др.), поэтому список служб не фиксируем.
_BAD_SUFFIXES = (".local", ".internal", ".lan", ".localhost", ".svc", ".cluster.local")


def _endpoint_ok(url: str) -> bool:
    import ipaddress
    try:
        u = urlparse(url)
    except Exception:
        return False
    host = (u.hostname or "").lower().rstrip(".")
    if u.scheme != "https" or not host or "." not in host or host == "localhost" or host.endswith(_BAD_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(host)
        return False  # адрес-IP вместо имени службы
    except ValueError:
        return True


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
async def public_key(db: AsyncSession = Depends(get_db), _: User = Depends(require_parent)):
    return {"key": await webpush.vapid_public(db)}


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db), user: User = Depends(require_parent)):
    n = await db.scalar(select(func.count(PushSubscription.id)).where(PushSubscription.user_id == user.id))
    return {"devices": int(n or 0)}


@router.post("/subscribe")
async def subscribe(body: SubIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_parent)):
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
        # не больше 10 устройств на человека — самые старые подписки убираем
        old = (await db.execute(select(PushSubscription.id).where(PushSubscription.user_id == user.id)
                                .order_by(PushSubscription.id.desc()).offset(9))).scalars().all()
        if old:
            await db.execute(delete(PushSubscription).where(PushSubscription.id.in_(old)))
        sub = PushSubscription(endpoint=body.endpoint)
        db.add(sub)
    sub.user_id, sub.p256dh, sub.auth = user.id, body.keys.p256dh, body.keys.auth
    sub.user_agent, sub.fails = (body.user_agent or "")[:300] or None, 0
    await db.commit()
    return {"ok": True}


@router.post("/unsubscribe")
async def unsubscribe(body: UnsubIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_parent)):
    await db.execute(delete(PushSubscription).where(PushSubscription.endpoint == body.endpoint,
                                                    PushSubscription.user_id == user.id))
    await db.commit()
    return {"ok": True}


@router.post("/test")
async def test(db: AsyncSession = Depends(get_db), user: User = Depends(require_parent)):
    subs = (await db.execute(select(PushSubscription).where(PushSubscription.user_id == user.id))).scalars().all()
    if not subs:
        raise HTTPException(status_code=400, detail="На ваших устройствах уведомления ещё не включены")
    if user.role == RoleEnum.parent:
        text_ = "Уведомления работают. Сюда придёт напоминание, если накопится задолженность по оплате занятий."
    else:
        text_ = "Уведомления работают. Сюда будут сразу приходить новые заявки, новые ученики, новые клиенты и отказы."
    res = await webpush.send_to(db, subs, "Пифагор: проверка", text_, "/", "test")
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
