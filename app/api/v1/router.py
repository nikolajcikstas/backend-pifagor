from app.api.v1.endpoints import auth, cabinet, lessons, public
from fastapi import APIRouter
from app.api.v1.endpoints import users
from app.api.v1.endpoints import files

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(lessons.router)
api_router.include_router(public.router)
api_router.include_router(cabinet.router)
api_router.include_router(files.router)
