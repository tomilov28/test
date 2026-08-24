import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.config import get_settings
from app.db import SessionLocal
from app.workflow.dispatcher import OutboxDispatcher
from app.workflow.reconciler import Reconciler
from app.workflow.registry import build_adapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

_settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    dispatcher = OutboxDispatcher(SessionLocal, build_adapter)
    reconciler = Reconciler(SessionLocal, build_adapter)
    if _settings.outbox_dispatcher_enabled:
        dispatcher.start()
    if _settings.reconciler_enabled:
        reconciler.start()
    yield
    dispatcher.stop()
    reconciler.stop()


app = FastAPI(title="Workflow Benchmark Harness", version="0.1.0", lifespan=lifespan)
app.include_router(router)
