"""Веб-уведомления (Web Push) без сторонних сервисов и библиотек.

• Ключи VAPID (RFC 8292) создаются один раз и хранятся в таблице app_state.
• Текст уведомления шифруется по RFC 8291 (aes128gcm) открытым ключом
  браузера — служба доставки (Google, Apple, Mozilla) содержимое не видит.
• Пилот: уведомления получают только администраторы и только о важном
  (новые заявки и ученики, клиенты, отказы, итоги дня).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, select

logger = logging.getLogger(__name__)

SUBJECT = "https://pifagor.by"
_TASKS: set = set()
_KEY_CACHE: dict = {}


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    s = (s or "").strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hmac(key: bytes, data: bytes) -> bytes:
    h = hmac.HMAC(key, hashes.SHA256())
    h.update(data)
    return h.finalize()


def _pub_raw(pub) -> bytes:
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


# ─── ключи VAPID ─────────────────────────────────────────────────────────────

async def vapid_key(db) -> ec.EllipticCurvePrivateKey:
    if "key" in _KEY_CACHE:
        return _KEY_CACHE["key"]
    from app.models.models import AppState
    row = await db.get(AppState, "vapid_private")
    if row is None:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        from sqlalchemy.dialects.postgresql import insert
        await db.execute(insert(AppState).values(key="vapid_private", value=pem, updated_at=datetime.utcnow())
                         .on_conflict_do_nothing(index_elements=["key"]))
        await db.commit()
        row = await db.get(AppState, "vapid_private")
    key = serialization.load_pem_private_key(row.value.encode(), password=None)
    _KEY_CACHE["key"] = key
    return key


async def vapid_public(db) -> str:
    return b64u(_pub_raw((await vapid_key(db)).public_key()))


def vapid_auth(endpoint: str, key: ec.EllipticCurvePrivateKey, now: Optional[int] = None) -> str:
    u = urlparse(endpoint)
    header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(now or time.time()) + 12 * 3600,
                              "sub": SUBJECT}, separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"vapid t={header}.{claims}.{b64u(sig)}, k={b64u(_pub_raw(key.public_key()))}"


# ─── шифрование содержимого (RFC 8291, aes128gcm) ────────────────────────────

def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: Optional[bytes] = None,
            server_key: Optional[ec.EllipticCurvePrivateKey] = None) -> bytes:
    ua_pub_raw = b64u_dec(p256dh)
    auth_secret = b64u_dec(auth)
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub_raw)
    as_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_pub_raw = _pub_raw(as_key.public_key())
    ecdh = as_key.exchange(ec.ECDH(), ua_pub)
    prk_key = _hmac(auth_secret, ecdh)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_pub_raw + as_pub_raw + b"\x01")
    salt = salt or os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    ct = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    rs = 4096
    return salt + struct.pack("!IB", rs, len(as_pub_raw)) + as_pub_raw + ct


# ─── отправка ────────────────────────────────────────────────────────────────

async def send_to(db, subs, title: str, body: str, url: str = "/", tag: Optional[str] = None) -> dict:
    from app.models.models import PushSubscription
    key = await vapid_key(db)
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag}, ensure_ascii=False).encode()
    ok = gone = failed = 0
    async with httpx.AsyncClient(timeout=10) as client:
        async def one(sub):
            try:
                data = encrypt(payload, sub.p256dh, sub.auth)
                r = await client.post(sub.endpoint, content=data, headers={
                    "Authorization": vapid_auth(sub.endpoint, key), "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream", "TTL": "86400", "Urgency": "normal",
                })
                return sub, r.status_code, r.text[:200]
            except Exception as e:  # сеть, неверный ключ подписки и т.п.
                return sub, 0, f"{type(e).__name__}: {e}"[:200]
        results = await asyncio.gather(*(one(s) for s in subs))
    now = datetime.utcnow()
    for sub, code, text in results:
        if 200 <= code < 300:
            ok += 1
            sub.last_ok_at, sub.fails = now, 0
        elif code in (404, 410):
            gone += 1
            await db.execute(delete(PushSubscription).where(PushSubscription.id == sub.id))
        else:
            failed += 1
            sub.fails = (sub.fails or 0) + 1
            logger.warning("web push failed %s: %s", code, text)
    await db.commit()
    return {"sent": ok, "removed": gone, "failed": failed}


async def push_admins(title: str, body: str, url: str = "/", tag: Optional[str] = None) -> dict:
    """Отправить уведомление всем подписанным администраторам (своя сессия БД)."""
    from app.db.session import AsyncSessionLocal
    from app.models.models import PushSubscription, RoleEnum, User
    async with AsyncSessionLocal() as db:
        subs = (await db.execute(
            select(PushSubscription).join(User, User.id == PushSubscription.user_id)
            .where(User.role == RoleEnum.admin, User.is_active.isnot(False))
        )).scalars().all()
        if not subs:
            return {"sent": 0, "removed": 0, "failed": 0}
        return await send_to(db, subs, title, body, url, tag)


def push_admins_later(title: str, body: str, url: str = "/", tag: Optional[str] = None) -> None:
    """Не ждать отправку: уведомление уходит в фоне и не задерживает ответ."""
    async def run():
        try:
            await push_admins(title, body, url, tag)
        except Exception:
            logger.exception("web push to admins failed")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    t = loop.create_task(run())
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)
