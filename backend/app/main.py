import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import close_pool, get_pool, init_pool
from app.routers import auth, reservations, rides, stations, support, users, vehicles, wallet


def _configure_app_logging() -> None:
    """Configure the root logger so every logging.getLogger(__name__) emits INFO (and above) to stderr."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
        force=True,
    )


_configure_app_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Micro-mobility API lifespan: initializing database pool")
    await init_pool()
    yield
    await close_pool()


app = FastAPI(title="Micro-mobility API", lifespan=lifespan)


@app.middleware("http")
async def _debug_log_every_request(request: Request, call_next):
    if settings.debug_http_log:
        print(f"[api-request] {request.method} {request.url.path}", flush=True)
    return await call_next(request)


origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """Return a proper JSON 500 so CORSMiddleware can attach its headers."""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
    )

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(vehicles.router)
app.include_router(stations.router)
app.include_router(rides.router)
app.include_router(wallet.router)
app.include_router(reservations.router)
app.include_router(support.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/db")
async def health_db():
    """Same connection as the API: use this to verify public.users columns vs Neon SQL editor."""
    pool = get_pool()
    async with pool.acquire() as conn:
        db_row = await conn.fetchrow("SELECT current_database() AS db, current_user AS db_user")
        cols = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'users'
            ORDER BY ordinal_position
            """
        )
    return {
        "database": db_row["db"] if db_row else None,
        "connected_as": db_row["db_user"] if db_row else None,
        "public_users_column_count": len(cols),
        "public_users_columns": [
            {"name": c["column_name"], "type": c["data_type"], "nullable": c["is_nullable"]} for c in cols
        ],
        "signup_expects": ["user_id", "email", "password_hash", "name", "phone_number", "status"],
    }
