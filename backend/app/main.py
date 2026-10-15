from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.application.registry_ledger import verify_registry_ledger
from app.core.config import get_settings
from app.core.correlation import CorrelationMiddleware
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.db.session import engine

settings = get_settings()
configure_logging(settings.log_level)

@asynccontextmanager
async def lifespan(application: FastAPI):
    await verify_registry_ledger(engine)
    yield


app = FastAPI(title="DCIM Platform API", version="0.1.0", openapi_url=f"{settings.api_v1_prefix}/openapi.json", lifespan=lifespan)

app.add_middleware(CorrelationMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)
app.include_router(api_router, prefix=settings.api_v1_prefix)
