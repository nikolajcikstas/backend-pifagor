# Пифагор — Backend API

## Быстрый старт

### 1. Docker (рекомендуется)
```bash
docker-compose up --build
```
API будет на http://localhost:8000  
Swagger UI: http://localhost:8000/docs

### 2. Локально (без Docker)
```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Запустить PostgreSQL отдельно, затем:
export DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/pifagor"
uvicorn app.main:app --reload
```

### 3. Заполнить базу тестовыми данными
```bash
cd backend
python seed.py
```

Тестовые аккаунты после seed:
| Email | Пароль | Роль |
|---|---|---|
| admin@pifagor.ru | admin123 | Администратор |
| tutor@pifagor.ru | tutor123 | Репетитор |
| parent@pifagor.ru | parent123 | Родитель |
| child@pifagor.ru | child123 | Ребёнок |

---

## Структура API

### Публичные (без авторизации)
| Метод | Путь | Описание |
|---|---|---|
| POST | /api/v1/auth/login | Войти |
| POST | /api/v1/auth/register | Зарегистрироваться |
| POST | /api/v1/auth/refresh | Обновить токен |
| GET | /api/v1/subjects | Список предметов |
| GET | /api/v1/subjects/{slug} | Предмет по slug |
| GET | /api/v1/prices | Цены |
| GET | /api/v1/reviews | Отзывы |
| GET | /api/v1/faq | FAQ |
| POST | /api/v1/requests | Заявка на бесплатное занятие |
| GET | /api/v1/users/tutors/public | Опубликованные репетиторы |

### Личный кабинет (требуют JWT)
| Метод | Путь | Роль | Описание |
|---|---|---|---|
| GET | /api/v1/users/me | все | Мой профиль |
| GET | /api/v1/lessons | все | Расписание |
| POST | /api/v1/lessons | tutor/admin | Создать занятие |
| PATCH | /api/v1/lessons/{id} | tutor/admin | Изменить занятие (статус, перенос) |
| GET | /api/v1/reports | tutor/parent/admin | Отчёты |
| POST | /api/v1/reports | tutor | Загрузить отчёт |
| GET | /api/v1/homeworks | child/tutor | Домашние задания |
| POST | /api/v1/homeworks | tutor | Создать ДЗ |
| PATCH | /api/v1/homeworks/{id}/submit | child | Сдать ДЗ |
| GET | /api/v1/payments | parent/admin | Оплаты |
| GET | /api/v1/tests | tutor/admin | Тесты |
| POST | /api/v1/tests | tutor | Создать тест |
| GET | /api/v1/notifications | все | Уведомления |
| POST | /api/v1/comments | parent | Комментарий репетитору |
| GET | /api/v1/acts | tutor/admin | Акты |
| GET | /api/v1/contracts/parent | parent/admin | Договоры |
| GET | /api/v1/contracts/tutor | tutor/admin | Договоры подряда |

---

## Структура проекта
```
backend/
├── app/
│   ├── main.py              # FastAPI app
│   ├── api/v1/
│   │   ├── router.py        # Сборный роутер
│   │   └── endpoints/
│   │       ├── auth.py      # Авторизация
│   │       ├── users.py     # Пользователи и профили
│   │       ├── lessons.py   # Расписание
│   │       ├── public.py    # Публичный сайт
│   │       └── cabinet.py   # Личный кабинет
│   ├── core/
│   │   ├── config.py        # Настройки
│   │   ├── security.py      # JWT, bcrypt
│   │   └── deps.py          # FastAPI dependencies (get_current_user, require_role)
│   ├── db/session.py        # AsyncSession, Base
│   ├── models/models.py     # Все SQLAlchemy модели
│   └── schemas/schemas.py   # Все Pydantic схемы
├── seed.py                  # Тестовые данные
├── requirements.txt
└── Dockerfile
```
