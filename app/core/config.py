from pydantic_settings import BaseSettings


def normalize_database_url(url: str) -> str:
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if "sslmode=require" in url:
        url = url.replace("sslmode=require", "ssl=require")
    if "channel_binding=require" in url:
        url = (
            url.replace("&channel_binding=require", "")
            .replace("?channel_binding=require&", "?")
            .replace("?channel_binding=require", "")
        )
    return url


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://pifagor:pifagor_pass@localhost:5432/pifagor"
    SECRET_KEY: str = "super-secret-key-change-in-prod"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    NOTIFICATION_EMAIL: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
settings.DATABASE_URL = normalize_database_url(settings.DATABASE_URL)
