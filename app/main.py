from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.limiter import limiter
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.memory.factory import create_memory_backend
from app.services.database import Database

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info("app_startup", env=settings.app_env)

    db = Database()
    await db.connect()
    app.state.db = db

    try:
        memory = create_memory_backend(settings)
    except Exception:
        logger.warning(
            "memory_backend_init_failed",
            backend=settings.memory_backend,
            hint="Set OPENAI_API_KEY for mem0 embeddings",
        )
        memory = None
    app.state.memory = memory
    app.state.lorebook = None
    app.state.active_character = None

    logger.info(
        "app_ready",
        memory_backend=settings.memory_backend,
        default_model=settings.default_model,
    )

    yield

    await db.close()
    logger.info("app_shutdown")


app = FastAPI(
    title="Yang-Ban",
    description="RisuAI Character Proxy with Observability",
    version="0.1.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)
