"""Реферальные ссылки pifagor.by/r/<код>.

• Код выдаётся ученику, когда ему ставят статус «Клиент» (и всем нынешним
  клиентам при первом открытии CRM). Код постоянный, ссылка ушедшего клиента
  продолжает работать.
• Считаются отправленные заявки (не переходы). Не засчитываются: тестовые
  (из браузера, где открыта CRM), повторный телефон, «свой» номер
  рекомендателя, телефон уже есть в CRM, отмеченные «Не считать».
• «Пришёл» — приглашённый ученик получил статус «Клиент». Связь «кто кого
  привёл» хранится отдельно (ReferralLink), а в комментарии ученика видна
  строка «Приглашён(а): Фамилия Имя». Менеджер может сам написать в
  комментарии «Пригласил: Фамилия Имя» — система распознает и засчитает.
"""
import re
import secrets
from datetime import datetime
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.models import (
    ChildProfile, CrmStatusEvent, LeadRequest, ParentChild, ParentProfile, ReferralLink, RoleEnum, User,
)

CLIENT = "Клиент"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих O/0, I/1
FLAG_LABELS = {
    "test": "тестовая (отправлена из браузера с открытой CRM)",
    "duplicate": "повторный телефон",
    "own": "телефон самого рекомендателя",
    "in_crm": "телефон уже есть в CRM",
    "excluded": "отмечена «Не считать»",
}
COMMENT_RE = re.compile(
    r"^[ \t]*(?:пригласил[аи]?|приглаш[её]н(?:\(а\)|а)?|по\s+рекомендации)[ \t]*[:\-—–][ \t]*(.+?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


def norm_phone(p: Optional[str]) -> Optional[str]:
    """Последние 9 цифр номера: +375 29 123-45-67, 80291234567 и 291234567 — одно и то же."""
    digits = "".join(ch for ch in (p or "") if ch.isdigit())
    if len(digits) < 9 or set(digits[-9:]) <= {"0"}:
        return None
    return digits[-9:]


def norm_name(s: Optional[str]) -> str:
    return " ".join((s or "").lower().replace("ё", "е").split())


def person_name(u: Optional[User]) -> str:
    return f"{(u.last_name or '').strip()} {(u.first_name or '').strip()}".strip() if u else ""


async def ensure_codes(db: AsyncSession) -> int:
    """Код для каждого клиента, у которого его ещё нет."""
    rows = (await db.execute(
        select(ChildProfile).where(ChildProfile.crm_status == CLIENT, ChildProfile.ref_code.is_(None))
    )).scalars().all()
    if not rows:
        return 0
    used = set((await db.execute(select(ChildProfile.ref_code).where(ChildProfile.ref_code.isnot(None)))).scalars().all())
    for ch in rows:
        code = new_code()
        while code in used:
            code = new_code()
        used.add(code)
        ch.ref_code = code
    await db.commit()
    return len(rows)


async def family_phones(db: AsyncSession, child_id: int) -> set[str]:
    """Телефоны ученика и его родителей (нормализованные)."""
    out: set[str] = set()
    child = (await db.execute(
        select(ChildProfile).options(
            joinedload(ChildProfile.user),
            joinedload(ChildProfile.parents).joinedload(ParentChild.parent).joinedload(ParentProfile.user),
        ).where(ChildProfile.id == child_id)
    )).scalars().unique().one_or_none()
    if not child:
        return out
    for u in [child.user] + [l.parent.user for l in child.parents if l.parent and l.parent.user]:
        n = norm_phone(u.phone if u else None)
        if n:
            out.add(n)
    return out


async def crm_phones(db: AsyncSession) -> set[str]:
    """Все телефоны учеников и родителей в CRM."""
    phones = (await db.execute(
        select(User.phone).where(User.phone.isnot(None), User.role.in_([RoleEnum.child, RoleEnum.parent]))
    )).scalars().all()
    return {n for n in (norm_phone(p) for p in phones) if n}


async def lead_flag(db: AsyncSession, referrer_id: int, phone_n: Optional[str], staff: bool) -> Optional[str]:
    if staff:
        return "test"
    if phone_n:
        if phone_n in await family_phones(db, referrer_id):
            return "own"
        if phone_n in await crm_phones(db):
            return "in_crm"
        dup = await db.scalar(select(func.count(LeadRequest.id)).where(
            LeadRequest.phone_norm == phone_n, LeadRequest.ref_code.isnot(None), LeadRequest.ref_flag.is_(None)))
        if dup:
            return "duplicate"
    return None


async def ever_clients(db: AsyncSession, child_ids) -> set[int]:
    ids = list(set(child_ids))
    if not ids:
        return set()
    cur = set((await db.execute(
        select(ChildProfile.id).where(ChildProfile.id.in_(ids), ChildProfile.crm_status == CLIENT)
    )).scalars().all())
    past = set((await db.execute(
        select(CrmStatusEvent.child_id).where(CrmStatusEvent.child_id.in_(ids), CrmStatusEvent.new_status == CLIENT).distinct()
    )).scalars().all())
    return cur | past


async def referral_stats(db: AsyncSession) -> dict[int, dict]:
    """По каждому рекомендателю: заявок (засчитанных / всего), приглашённых, стали клиентами."""
    out: dict[int, dict] = {}
    for rid, flag, n in (await db.execute(
        select(LeadRequest.referrer_child_id, LeadRequest.ref_flag, func.count(LeadRequest.id))
        .where(LeadRequest.referrer_child_id.isnot(None)).group_by(LeadRequest.referrer_child_id, LeadRequest.ref_flag)
    )).all():
        s = out.setdefault(rid, {"leads": 0, "leads_total": 0, "invited": 0, "clients": 0})
        s["leads_total"] += n
        if flag is None:
            s["leads"] += n
    links = (await db.execute(select(ReferralLink.referrer_child_id, ReferralLink.child_id))).all()
    clients = await ever_clients(db, [c for _, c in links])
    for rid, cid in links:
        s = out.setdefault(rid, {"leads": 0, "leads_total": 0, "invited": 0, "clients": 0})
        s["invited"] += 1
        if cid in clients:
            s["clients"] += 1
    return out


async def _find_referrer_by_name(db: AsyncSession, name: str, exclude_child: int) -> list[tuple[int, str]]:
    target = norm_name(name)
    if not target:
        return []
    rows = (await db.execute(
        select(ChildProfile.id, User.last_name, User.first_name).join(User, User.id == ChildProfile.user_id)
        .where(ChildProfile.id != exclude_child)
    )).all()
    found = []
    for cid, ln, fn in rows:
        a = norm_name(f"{ln} {fn}")
        b = norm_name(f"{fn} {ln}")
        if target in (a, b):
            found.append((cid, f"{(ln or '').strip()} {(fn or '').strip()}".strip()))
    return found


async def process_child_referral(db: AsyncSession, child: ChildProfile, student: User,
                                 parent_users: list[User]) -> Optional[str]:
    """Вызывается при сохранении карточки ученика (до commit). Возвращает
    сообщение для менеджера или None.
    1) Комментарий содержит «Пригласил: Фамилия Имя» — связываем с этим клиентом.
    2) Иначе, если ученик ещё ни с кем не связан, ищем реферальную заявку с его
       телефоном/телефоном родителя или его именем — связываем и дописываем
       в комментарий «Приглашён(а): Фамилия Имя»."""
    link = (await db.execute(select(ReferralLink).where(ReferralLink.child_id == child.id))).scalars().first()
    m = COMMENT_RE.search(child.notes or "")
    if m:
        name = m.group(1)
        found = await _find_referrer_by_name(db, name, child.id)
        if len(found) == 1:
            rid, rname = found[0]
            if link and link.referrer_child_id == rid:
                return None
            if link:
                link.referrer_child_id, link.source, link.lead_id = rid, "comment", None
            else:
                db.add(ReferralLink(child_id=child.id, referrer_child_id=rid, source="comment", created_at=datetime.utcnow()))
            return f"Засчитано приглашение: {rname}"
        if not found:
            return f"В комментарии указан «{name}», но такого ученика в CRM нет — проверьте написание (Фамилия Имя)"
        return f"Учеников с именем «{name}» несколько — уточните, кого засчитать"
    if link and link.source == "comment":
        await db.delete(link)  # строку «Пригласил: …» убрали из комментария
        return "Приглашение снято: строки «Пригласил: …» в комментарии больше нет"
    if link:
        return None
    phones = {n for n in (norm_phone(u.phone) for u in [student] + parent_users if u) if n}
    conds = []
    if phones:
        conds.append(LeadRequest.phone_norm.in_(phones))
    sname = norm_name(person_name(student))
    leads = (await db.execute(
        select(LeadRequest).where(LeadRequest.referrer_child_id.isnot(None), LeadRequest.referrer_child_id != child.id)
        .order_by(LeadRequest.created_at.desc())
    )).scalars().all()
    lead = None
    for x in leads:
        if x.ref_flag in ("test", "own", "excluded"):
            continue
        if (x.phone_norm and x.phone_norm in phones) or (
                sname and x.child_name and norm_name(x.child_name) in (sname, norm_name(f"{student.first_name} {student.last_name}"))):
            lead = x
            break
    if not lead:
        return None
    referrer = (await db.execute(
        select(User).join(ChildProfile, ChildProfile.user_id == User.id).where(ChildProfile.id == lead.referrer_child_id)
    )).scalars().first()
    rname = person_name(referrer)
    db.add(ReferralLink(child_id=child.id, referrer_child_id=lead.referrer_child_id, lead_id=lead.id,
                        source="form", created_at=datetime.utcnow()))
    line = f"Приглашён(а): {rname}"
    child.notes = (child.notes + "\n" + line) if (child.notes or "").strip() else line
    return f"Найдена заявка по рекомендации — {line}"
