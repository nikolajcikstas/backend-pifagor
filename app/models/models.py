import enum
from datetime import date, datetime, time
from typing import List, Optional

from sqlalchemy import (
    Boolean, Date, DateTime, Enum, ForeignKey,
    Integer, String, Text, Time, Float, func
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


# ─── Enums ────────────────────────────────────────────────────────────────────

class RoleEnum(str, enum.Enum):
    admin = "admin"
    tutor = "tutor"
    parent = "parent"
    child = "child"


class LessonStatus(str, enum.Enum):
    scheduled = "scheduled"
    completed = "completed"
    cancelled = "cancelled"
    rescheduled = "rescheduled"


class RequestStatus(str, enum.Enum):
    new = "new"
    processed = "processed"
    rejected = "rejected"


# ─── Users & Auth ─────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[RoleEnum] = mapped_column(Enum(RoleEnum), nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    middle_name: Mapped[Optional[str]] = mapped_column(String(100))
    phone: Mapped[Optional[str]] = mapped_column(String(30))
    avatar_url: Mapped[Optional[str]] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relations
    tutor_profile: Mapped[Optional["TutorProfile"]] = relationship(back_populates="user", uselist=False)
    parent_profile: Mapped[Optional["ParentProfile"]] = relationship(back_populates="user", uselist=False)
    child_profile: Mapped[Optional["ChildProfile"]] = relationship(back_populates="user", uselist=False)
    notifications: Mapped[List["Notification"]] = relationship(back_populates="user")

    @property
    def full_name(self) -> str:
        parts = [self.last_name, self.first_name]
        if self.middle_name:
            parts.append(self.middle_name)
        return " ".join(parts)


# ─── Profiles ─────────────────────────────────────────────────────────────────

class TutorProfile(Base):
    __tablename__ = "tutor_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    bio: Mapped[Optional[str]] = mapped_column(Text)
    education: Mapped[Optional[str]] = mapped_column(Text)
    experience_years: Mapped[Optional[int]] = mapped_column(Integer)
    rate_per_hour: Mapped[Optional[float]] = mapped_column(Float)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["User"] = relationship(back_populates="tutor_profile")
    subjects: Mapped[List["TutorSubject"]] = relationship(back_populates="tutor")
    lessons: Mapped[List["Lesson"]] = relationship(back_populates="tutor")
    contracts: Mapped[List["TutorContract"]] = relationship(back_populates="tutor")
    reports: Mapped[List["Report"]] = relationship(back_populates="tutor")


class ParentProfile(Base):
    __tablename__ = "parent_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)

    user: Mapped["User"] = relationship(back_populates="parent_profile")
    children: Mapped[List["ParentChild"]] = relationship(back_populates="parent")
    payments: Mapped[List["Payment"]] = relationship(back_populates="parent")
    contracts: Mapped[List["ParentContract"]] = relationship(back_populates="parent")
    comments: Mapped[List["Comment"]] = relationship(back_populates="parent")


class ChildProfile(Base):
    __tablename__ = "child_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    grade: Mapped[Optional[int]] = mapped_column(Integer)

    user: Mapped["User"] = relationship(back_populates="child_profile")
    parents: Mapped[List["ParentChild"]] = relationship(back_populates="child")
    lessons: Mapped[List["Lesson"]] = relationship(back_populates="child")
    homeworks: Mapped[List["Homework"]] = relationship(back_populates="child")
    materials: Mapped[List["Material"]] = relationship(back_populates="child")


class ParentChild(Base):
    """Many-to-many: parent ↔ child"""
    __tablename__ = "parent_children"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("parent_profiles.id"))
    child_id: Mapped[int] = mapped_column(ForeignKey("child_profiles.id"))

    parent: Mapped["ParentProfile"] = relationship(back_populates="children")
    child: Mapped["ChildProfile"] = relationship(back_populates="parents")


# ─── Subjects ─────────────────────────────────────────────────────────────────

class Subject(Base):
    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    icon: Mapped[Optional[str]] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    tutors: Mapped[List["TutorSubject"]] = relationship(back_populates="subject")
    prices: Mapped[List["Price"]] = relationship(back_populates="subject")


class TutorSubject(Base):
    __tablename__ = "tutor_subjects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tutor_id: Mapped[int] = mapped_column(ForeignKey("tutor_profiles.id"))
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))

    tutor: Mapped["TutorProfile"] = relationship(back_populates="subjects")
    subject: Mapped["Subject"] = relationship(back_populates="tutors")


# ─── Lessons / Schedule ───────────────────────────────────────────────────────

class Lesson(Base):
    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tutor_id: Mapped[int] = mapped_column(ForeignKey("tutor_profiles.id"))
    child_id: Mapped[int] = mapped_column(ForeignKey("child_profiles.id"))
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    date: Mapped[date] = mapped_column(Date, nullable=False)
    time_start: Mapped[time] = mapped_column(Time, nullable=False)
    time_end: Mapped[time] = mapped_column(Time, nullable=False)
    status: Mapped[LessonStatus] = mapped_column(Enum(LessonStatus), default=LessonStatus.scheduled)
    cancel_reason: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    is_free_trial: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    tutor: Mapped["TutorProfile"] = relationship(back_populates="lessons")
    child: Mapped["ChildProfile"] = relationship(back_populates="lessons")
    subject: Mapped["Subject"] = relationship()
    homeworks: Mapped[List["Homework"]] = relationship(back_populates="lesson")


# ─── Reports ──────────────────────────────────────────────────────────────────

class Report(Base):
    """Tutor uploads a report every 5 lessons"""
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tutor_id: Mapped[int] = mapped_column(ForeignKey("tutor_profiles.id"))
    child_id: Mapped[int] = mapped_column(ForeignKey("child_profiles.id"))
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    lesson_count: Mapped[int] = mapped_column(Integer, default=5)
    file_url: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    tutor: Mapped["TutorProfile"] = relationship(back_populates="reports")
    child: Mapped["ChildProfile"] = relationship()
    subject: Mapped["Subject"] = relationship()


class TutorDocument(Base):
    """Документ, отправленный админом конкретному репетитору (PDF и т.п.)."""
    __tablename__ = "tutor_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tutor_id: Mapped[int] = mapped_column(ForeignKey("tutor_profiles.id"))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    file_url: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    tutor: Mapped["TutorProfile"] = relationship()


# ─── Homework / Materials ─────────────────────────────────────────────────────

class Homework(Base):
    __tablename__ = "homeworks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id"))
    child_id: Mapped[int] = mapped_column(ForeignKey("child_profiles.id"))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    file_url: Mapped[Optional[str]] = mapped_column(String(500))           # tutor uploads
    submission_url: Mapped[Optional[str]] = mapped_column(String(500))     # child submits
    is_done: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    lesson: Mapped["Lesson"] = relationship(back_populates="homeworks")
    child: Mapped["ChildProfile"] = relationship(back_populates="homeworks")


class Material(Base):
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tutor_id: Mapped[int] = mapped_column(ForeignKey("tutor_profiles.id"))
    child_id: Mapped[int] = mapped_column(ForeignKey("child_profiles.id"))
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    file_url: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    child: Mapped["ChildProfile"] = relationship(back_populates="materials")
    subject: Mapped["Subject"] = relationship()


# ─── Contracts ────────────────────────────────────────────────────────────────

class TutorContract(Base):
    """Договор подряда с репетитором"""
    __tablename__ = "tutor_contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tutor_id: Mapped[int] = mapped_column(ForeignKey("tutor_profiles.id"))
    file_url: Mapped[str] = mapped_column(String(500), nullable=False)
    signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    tutor: Mapped["TutorProfile"] = relationship(back_populates="contracts")


class ParentContract(Base):
    """Договор с родителем"""
    __tablename__ = "parent_contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("parent_profiles.id"))
    child_id: Mapped[int] = mapped_column(ForeignKey("child_profiles.id"))
    file_url: Mapped[str] = mapped_column(String(500), nullable=False)
    signed_file_url: Mapped[Optional[str]] = mapped_column(String(500))
    is_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    parent: Mapped["ParentProfile"] = relationship(back_populates="contracts")
    child: Mapped["ChildProfile"] = relationship()


# ─── Payments ─────────────────────────────────────────────────────────────────

class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("parent_profiles.id"))
    child_id: Mapped[int] = mapped_column(ForeignKey("child_profiles.id"))
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500))
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    period_start: Mapped[Optional[date]] = mapped_column(Date)
    period_end: Mapped[Optional[date]] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    parent: Mapped["ParentProfile"] = relationship(back_populates="payments")
    child: Mapped["ChildProfile"] = relationship()


# ─── Prices ───────────────────────────────────────────────────────────────────

class Price(Base):
    __tablename__ = "prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[Optional[int]] = mapped_column(ForeignKey("subjects.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    price_per_lesson: Mapped[float] = mapped_column(Float, nullable=False)
    lessons_in_package: Mapped[Optional[int]] = mapped_column(Integer)
    discount_percent: Mapped[Optional[float]] = mapped_column(Float)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    subject: Mapped[Optional["Subject"]] = relationship(back_populates="prices")


# ─── Reviews ──────────────────────────────────────────────────────────────────

class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("parent_profiles.id"))
    tutor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tutor_profiles.id"))
    author_name: Mapped[str] = mapped_column(String(200), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[int] = mapped_column(Integer, default=5)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ─── Free Lesson Requests ─────────────────────────────────────────────────────

class LeadRequest(Base):
    """Заявка с публичного сайта на бесплатное занятие"""
    __tablename__ = "lead_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(30), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255))
    subject_id: Mapped[Optional[int]] = mapped_column(ForeignKey("subjects.id"))
    message: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[RequestStatus] = mapped_column(Enum(RequestStatus), default=RequestStatus.new)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    subject: Mapped[Optional["Subject"]] = relationship()


# ─── FAQ ──────────────────────────────────────────────────────────────────────

class FAQ(Base):
    __tablename__ = "faqs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[Optional[int]] = mapped_column(ForeignKey("subjects.id"), nullable=True)
    order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    subject: Mapped[Optional["Subject"]] = relationship()


# ─── Notifications ────────────────────────────────────────────────────────────

class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="notifications")


# ─── Comments (parent → tutor) ────────────────────────────────────────────────

class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("parent_profiles.id"))
    tutor_id: Mapped[int] = mapped_column(ForeignKey("tutor_profiles.id"))
    child_id: Mapped[int] = mapped_column(ForeignKey("child_profiles.id"))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    parent: Mapped["ParentProfile"] = relationship(back_populates="comments")
    tutor: Mapped["TutorProfile"] = relationship()
    child: Mapped["ChildProfile"] = relationship()


# ─── Acts (акты выполненных работ) ───────────────────────────────────────────

class Act(Base):
    __tablename__ = "acts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tutor_id: Mapped[int] = mapped_column(ForeignKey("tutor_profiles.id"))
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    lessons_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_amount: Mapped[float] = mapped_column(Float, nullable=False)
    blank_url: Mapped[Optional[str]] = mapped_column(String(500))    # пустой акт для скачивания
    signed_url: Mapped[Optional[str]] = mapped_column(String(500))   # подписанный акт от репетитора
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    tutor: Mapped["TutorProfile"] = relationship()


# ─── Tests ────────────────────────────────────────────────────────────────────

class Test(Base):
    __tablename__ = "tests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tutor_id: Mapped[int] = mapped_column(ForeignKey("tutor_profiles.id"))
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    questions: Mapped[List["TestQuestion"]] = relationship(back_populates="test", cascade="all, delete-orphan")
    results: Mapped[List["TestResult"]] = relationship(back_populates="test")


class TestQuestion(Base):
    __tablename__ = "test_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.id"))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[str] = mapped_column(String(30), default="single")  # single, multiple, text
    order: Mapped[int] = mapped_column(Integer, default=0)

    test: Mapped["Test"] = relationship(back_populates="questions")
    answers: Mapped[List["TestAnswer"]] = relationship(back_populates="question", cascade="all, delete-orphan")


class TestAnswer(Base):
    __tablename__ = "test_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("test_questions.id"))
    text: Mapped[str] = mapped_column(String(500), nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False)

    question: Mapped["TestQuestion"] = relationship(back_populates="answers")


class TestResult(Base):
    __tablename__ = "test_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.id"))
    child_id: Mapped[int] = mapped_column(ForeignKey("child_profiles.id"))
    score: Mapped[Optional[float]] = mapped_column(Float)
    answers_json: Mapped[Optional[str]] = mapped_column(Text)  # JSON строка с ответами
    completed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    test: Mapped["Test"] = relationship(back_populates="results")
    child: Mapped["ChildProfile"] = relationship()


# ─── Invite Codes (Коды доступа) ──────────────────────────────────────────────

class InviteCode(Base):
    __tablename__ = "invite_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    role: Mapped[RoleEnum] = mapped_column(Enum(RoleEnum), nullable=False)  # tutor, child, parent
    description: Mapped[Optional[str]] = mapped_column(String(500))  # Описание (кому выдан)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False)

    # Связь для пары Ученик + Родитель (ссылается на ID этого же кода)
    linked_code_id: Mapped[Optional[int]] = mapped_column(ForeignKey("invite_codes.id"))

    # 🌟 ДОБАВЛЯЕМ СЮДА: Запоминаем, какой user_id активировал этот конкретный код
    used_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Релейшн на самого себя, чтобы удобно подтягивать пару из базы
    linked_code: Mapped[Optional["InviteCode"]] = relationship(remote_side=[id])


# ─── Email Receipts (чеки EasyPay) ────────────────────────────────────────────

class EmailReceipt(Base):
    """Parsed EasyPay payment receipts from email inbox."""
    __tablename__ = "email_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_number: Mapped[Optional[str]] = mapped_column(String(100))
    payer_name: Mapped[str] = mapped_column(String(300), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    payment_date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    # Linked child profile (matched by account number or name)
    child_id: Mapped[Optional[int]] = mapped_column(ForeignKey("child_profiles.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    child: Mapped[Optional["ChildProfile"]] = relationship()