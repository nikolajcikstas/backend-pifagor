from datetime import date, datetime, time
from typing import List, Optional
from pydantic import BaseModel, EmailStr, field_validator
from app.models.models import RoleEnum, LessonStatus, RequestStatus


# ─── Auth ─────────────────────────────────────────────────────────────────────

class InviteCodeCreate(BaseModel):
    role: str  # Принимает "tutor" или "pair" (для пары ученик+родитель)
    description: Optional[str] = None


class InviteCodeResponse(BaseModel):
    id: int
    code: str
    role: RoleEnum
    description: Optional[str] = None
    is_used: bool
    linked_code_id: Optional[int] = None
    created_at: datetime

    # Пишем model_config точно так же, как во всём твоем файле
    model_config = {"from_attributes": True}

class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenRefresh(BaseModel):
    refresh_token: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


# ─── User ─────────────────────────────────────────────────────────────────────

class UserBase(BaseModel):
    email: EmailStr
    first_name: str
    last_name: str
    middle_name: Optional[str] = None
    phone: Optional[str] = None


class UserCreate(UserBase):
    password: str
    role: RoleEnum
    invite_code: str


class UserUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    middle_name: Optional[str] = None
    phone: Optional[str] = None
    avatar_url: Optional[str] = None


class ProfileBasicOut(BaseModel):
    id: int
    lesson_price: Optional[float] = None
    model_config = {"from_attributes": True}


class UserOut(UserBase):
    id: int
    role: RoleEnum
    avatar_url: Optional[str] = None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UserOutWithProfiles(UserOut):
    """Used only where User.tutor_profile/child_profile/parent_profile are eager-loaded
    (e.g. admin user list) — do NOT use as a generic response_model, it will crash with
    MissingGreenlet if the relations aren't selectinload()'ed on the query."""
    tutor_profile: Optional[ProfileBasicOut] = None
    child_profile: Optional[ProfileBasicOut] = None
    parent_profile: Optional[ProfileBasicOut] = None


class UserMeOut(UserBase):
    """Extended user schema for /users/me — includes profile IDs."""
    id: int
    role: RoleEnum
    avatar_url: Optional[str] = None
    is_active: bool
    created_at: datetime
    tutor_profile: Optional[ProfileBasicOut] = None
    child_profile: Optional[ProfileBasicOut] = None
    parent_profile: Optional[ProfileBasicOut] = None

    model_config = {"from_attributes": True}


# ─── Subject ──────────────────────────────────────────────────────────────────

class SubjectOut(BaseModel):
    id: int
    name: str
    slug: str
    description: Optional[str] = None
    icon: Optional[str] = None
    is_active: bool

    model_config = {"from_attributes": True}


class SubjectCreate(BaseModel):
    name: str
    slug: str
    description: Optional[str] = None
    icon: Optional[str] = None


# ─── Tutor ────────────────────────────────────────────────────────────────────

class TutorProfileOut(BaseModel):
    id: int
    bio: Optional[str] = None
    education: Optional[str] = None
    experience_years: Optional[int] = None
    rate_per_hour: Optional[float] = None
    is_published: bool
    user: UserOut
    subjects: List[SubjectOut] = []

    model_config = {"from_attributes": True}


class TutorProfileUpdate(BaseModel):
    bio: Optional[str] = None
    education: Optional[str] = None
    experience_years: Optional[int] = None
    rate_per_hour: Optional[float] = None
    is_published: Optional[bool] = None


class AdminTutorUserUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    avatar_url: Optional[str] = None


class AdminTutorUpdate(BaseModel):
    """Used by admin to edit a tutor profile (incl. linked user fields and subjects)."""
    bio: Optional[str] = None
    education: Optional[str] = None
    experience_years: Optional[int] = None
    rate_per_hour: Optional[float] = None
    is_published: Optional[bool] = None
    subject_ids: Optional[List[int]] = None
    user: Optional[AdminTutorUserUpdate] = None


# ─── Lesson ───────────────────────────────────────────────────────────────────

class LessonCreate(BaseModel):
    tutor_id: int
    child_id: int
    subject_id: int
    date: date
    time_start: time
    time_end: time
    is_free_trial: bool = False
    notes: Optional[str] = None


class LessonUpdate(BaseModel):
    date: Optional[date] = None
    time_start: Optional[time] = None
    time_end: Optional[time] = None
    status: Optional[LessonStatus] = None
    cancel_reason: Optional[str] = None
    notes: Optional[str] = None


class LessonOut(BaseModel):
    id: int
    tutor_id: int
    child_id: int
    subject_id: int
    student_name: Optional[str] = "Ученик"
    tutor_name: Optional[str] = "Репетитор"
    subject_name: Optional[str] = "Предмет"
    date: date
    time_start: time
    time_end: time
    status: LessonStatus
    cancel_reason: Optional[str] = None
    notes: Optional[str] = None
    is_free_trial: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Report ───────────────────────────────────────────────────────────────────

class ReportCreate(BaseModel):
    child_id: int
    subject_id: int
    content: str
    lesson_count: int = 5
    file_url: Optional[str] = None


class ReportOut(BaseModel):
    id: int
    tutor_id: int
    child_id: int
    subject_id: int
    content: str
    lesson_count: int
    file_url: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Tutor documents (admin → tutor) ───────────────────────────────────────────

class TutorDocumentCreate(BaseModel):
    tutor_id: int
    title: str
    file_url: str


class TutorDocumentOut(BaseModel):
    id: int
    tutor_id: int
    title: str
    file_url: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Homework ─────────────────────────────────────────────────────────────────

class HomeworkCreate(BaseModel):
    lesson_id: int
    child_id: int
    description: str


class HomeworkOut(BaseModel):
    id: int
    lesson_id: int
    child_id: int
    description: str
    file_url: Optional[str] = None
    submission_url: Optional[str] = None
    is_done: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Payment ──────────────────────────────────────────────────────────────────

class PaymentCreate(BaseModel):
    parent_id: int
    child_id: int
    amount: float
    description: Optional[str] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None


class PaymentOut(BaseModel):
    id: int
    parent_id: int
    child_id: int
    amount: float
    description: Optional[str] = None
    is_paid: bool
    paid_at: Optional[datetime] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Price ────────────────────────────────────────────────────────────────────

class PriceOut(BaseModel):
    id: int
    subject_id: Optional[int] = None
    title: str
    description: Optional[str] = None
    price_per_lesson: float
    lessons_in_package: Optional[int] = None
    discount_percent: Optional[float] = None
    is_active: bool

    model_config = {"from_attributes": True}


class PriceCreate(BaseModel):
    subject_id: Optional[int] = None
    title: str
    description: Optional[str] = None
    price_per_lesson: float
    lessons_in_package: Optional[int] = None
    discount_percent: Optional[float] = None


# ─── Review ───────────────────────────────────────────────────────────────────

class ReviewCreate(BaseModel):
    author_name: str
    text: str
    rating: int = 5
    tutor_id: Optional[int] = None

    @field_validator("rating")
    @classmethod
    def rating_range(cls, v):
        if not 1 <= v <= 5:
            raise ValueError("Rating must be between 1 and 5")
        return v


class ReviewOut(BaseModel):
    id: int
    author_name: str
    text: str
    rating: int
    tutor_id: Optional[int] = None
    is_published: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── LeadRequest ──────────────────────────────────────────────────────────────

class LeadRequestCreate(BaseModel):
    name: str
    phone: str
    email: Optional[EmailStr] = None
    subject_id: Optional[int] = None
    message: Optional[str] = None


class LeadRequestOut(BaseModel):
    id: int
    name: str
    phone: str
    email: Optional[str] = None
    subject_id: Optional[int] = None
    message: Optional[str] = None
    status: RequestStatus
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── FAQ ──────────────────────────────────────────────────────────────────────

class FAQOut(BaseModel):
    id: int
    question: str
    answer: str
    subject_id: Optional[int] = None
    order: int

    model_config = {"from_attributes": True}


class FAQCreate(BaseModel):
    question: str
    answer: str
    subject_id: Optional[int] = None
    order: int = 0


# ─── Test ─────────────────────────────────────────────────────────────────────

class TestAnswerCreate(BaseModel):
    text: str
    is_correct: bool = False


class TestAnswerOut(BaseModel):
    id: int
    text: str
    is_correct: bool

    model_config = {"from_attributes": True}


class TestQuestionCreate(BaseModel):
    text: str
    question_type: str = "single"
    order: int = 0
    answers: List[TestAnswerCreate] = []


class TestQuestionOut(BaseModel):
    id: int
    text: str
    question_type: str
    order: int
    answers: List[TestAnswerOut] = []

    model_config = {"from_attributes": True}


class TestCreate(BaseModel):
    subject_id: int
    title: str
    description: Optional[str] = None
    questions: List[TestQuestionCreate] = []


class TestOut(BaseModel):
    id: int
    tutor_id: int
    subject_id: int
    title: str
    description: Optional[str] = None
    is_active: bool
    created_at: datetime
    questions: List[TestQuestionOut] = []

    model_config = {"from_attributes": True}


class TestResultCreate(BaseModel):
    test_id: int
    child_id: int
    answers_json: Optional[str] = None


class TestResultOut(BaseModel):
    id: int
    test_id: int
    child_id: int
    score: Optional[float] = None
    completed_at: datetime

    model_config = {"from_attributes": True}


# ─── Notification ─────────────────────────────────────────────────────────────

class NotificationOut(BaseModel):
    id: int
    title: str
    body: str
    is_read: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Comment ──────────────────────────────────────────────────────────────────

class CommentCreate(BaseModel):
    tutor_id: int
    child_id: int
    text: str


class CommentOut(BaseModel):
    id: int
    parent_id: int
    tutor_id: int
    child_id: int
    text: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Act ──────────────────────────────────────────────────────────────────────

class ActOut(BaseModel):
    id: int
    tutor_id: int
    period_start: date
    period_end: date
    lessons_count: int
    total_amount: float
    blank_url: Optional[str] = None
    signed_url: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Contract ─────────────────────────────────────────────────────────────────

class ParentContractOut(BaseModel):
    id: int
    parent_id: int
    child_id: int
    file_url: str
    signed_file_url: Optional[str] = None
    is_signed: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TutorContractOut(BaseModel):
    id: int
    tutor_id: int
    file_url: str
    signed_file_url: Optional[str] = None
    signed_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── EmailReceipt ──────────────────────────────────────────────────────────────

class EmailReceiptOut(BaseModel):
    id: int
    receipt_number: Optional[str] = None
    payer_name: str
    amount: float
    payment_date: Optional[datetime] = None
    child_id: Optional[int] = None
    student_name: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Finance Report ────────────────────────────────────────────────────────────

class StudentFinanceRow(BaseModel):
    child_id: int
    student_name: str
    lessons_conducted: int
    lessons_paid: int
    amount_paid: float
    lesson_price: float
