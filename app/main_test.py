from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.api.v1.router import api_router
from app.api.v1.endpoints import admin

@asynccontextmanager
async def lifespan(app):
    print("LIFESPAN START", flush=True)
    yield

app = FastAPI(lifespan=lifespan)
app.include_router(api_router)
app.include_router(admin.router, prefix="/api/v1/admin", tags=["admin"])

@app.get("/health")
async def health():
    return {"ok": True}
