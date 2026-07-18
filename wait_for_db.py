"""
Ждёт пока PostgreSQL будет готов принимать соединения.
Запускается перед uvicorn в docker-compose command.
"""
import asyncio
import os
import sys
import time

import asyncpg


async def wait():
    url = os.environ.get("DATABASE_URL", "")
    # asyncpg не понимает +asyncpg в URL — убираем
    url = url.replace("postgresql+asyncpg://", "postgresql://")

    print("⏳ Waiting for PostgreSQL...", flush=True)
    for attempt in range(30):
        try:
            conn = await asyncpg.connect(url)
            await conn.close()
            print("✅ PostgreSQL is ready!", flush=True)
            return
        except Exception as e:
            print(f"   attempt {attempt + 1}/30: {e}", flush=True)
            await asyncio.sleep(2)

    print("❌ PostgreSQL not available after 30 attempts. Exiting.", flush=True)
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(wait())
