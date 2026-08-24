"""Reconciler: engine active human tasks -> idempotent UPSERT of WorkItems.

Workflow engine owns the authoritative BPMN execution state (which wait states
are active). Our system mirrors that into WorkItems. This loop is the bridge:

    engine active Human Tasks
            |
          reconciler
            |
    idempotent UPSERT WorkItem

It must be:
  * runnable manually (POST /admin/reconcile)
  * runnable periodically (background thread)
  * idempotent (unique key request_id+task_definition_key+external_task_id)
  * restart-safe (stateless; no cursor state persisted)
"""

import logging
import threading
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain.enums import LifecycleState, RequestOutcome, WorkItemState
from app.domain.models import Request, WorkItem

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def upsert_work_item(db: Session, *, request_id: uuid.UUID, task_definition_key: str, external_task_id: str) -> WorkItem:
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        stmt = sqlite.insert(WorkItem)
    else:
        stmt = postgresql.insert(WorkItem)
    stmt = stmt.values(
        request_id=request_id,
        task_definition_key=task_definition_key,
        external_task_id=external_task_id,
        state=WorkItemState.ACTIVE.value,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["request_id", "task_definition_key", "external_task_id"]
    )
    db.execute(stmt)
    db.flush()
    return (
        db.execute(
            select(WorkItem).where(
                WorkItem.request_id == request_id,
                WorkItem.task_definition_key == task_definition_key,
                WorkItem.external_task_id == external_task_id,
            )
        )
        .scalars()
        .one()
    )


def reconcile_once(db: Session, adapter_factory, engine_filter: str | None = None) -> dict:
    summary = {
        "requests_seen": 0,
        "tasks_seen": 0,
        "work_items_upserted": 0,
        "completed_requests": 0,
        "errors": [],
    }

    stmt = select(Request).where(
        Request.lifecycle_state == LifecycleState.ACTIVE.value,
        Request.workflow_instance_id.is_not(None),
    )
    if engine_filter:
        stmt = stmt.where(Request.workflow_engine == engine_filter)
    requests = db.execute(stmt).scalars().all()

    for request in requests:
        summary["requests_seen"] += 1
        adapter = adapter_factory(request.workflow_engine)
        try:
            tasks = adapter.get_active_human_tasks(request.workflow_instance_id)
            for task in tasks:
                summary["tasks_seen"] += 1
                upsert_work_item(
                    db,
                    request_id=request.id,
                    task_definition_key=task.task_definition_key,
                    external_task_id=task.id,
                )
                summary["work_items_upserted"] += 1

            if request.workflow_instance_id:
                instance = adapter.get_process_instance(request.workflow_instance_id)
                if instance.state == "ENDED":
                    request.lifecycle_state = LifecycleState.CLOSED.value
                    request.outcome = RequestOutcome.COMPLETED.value
                    request.closed_at = _utcnow()
                    summary["completed_requests"] += 1

            db.commit()
        except NotImplementedError:
            db.rollback()
            logger.info(
                "reconciler: engine adapter for %s not implemented yet (bootstrap phase)",
                request.workflow_engine,
            )
        except Exception as exc:  # engine unreachable etc.
            db.rollback()
            summary["errors"].append(
                {"request_id": str(request.id), "engine": request.workflow_engine, "error": str(exc)}
            )
            logger.warning("reconciler: request %s failed: %s", request.id, exc)
        finally:
            try:
                adapter.close()
            except Exception:
                pass

    return summary


class Reconciler:
    def __init__(self, db_factory, adapter_factory, interval_seconds: float | None = None) -> None:
        self._db_factory = db_factory
        self._adapter_factory = adapter_factory
        self._interval = interval_seconds or get_settings().reconcile_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="reconciler", daemon=True)
        self._thread.start()
        logger.info("reconciler started (interval=%ss)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                db = self._db_factory()
                try:
                    summary = reconcile_once(db, self._adapter_factory)
                    if summary["errors"]:
                        logger.warning("reconciler pass summary: %s", summary)
                finally:
                    db.close()
            except Exception as exc:
                logger.error("reconciler loop error: %s", exc)
            self._stop.wait(self._interval)
