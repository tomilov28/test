"""Flowable external worker (Flowable "external job" pattern).

The `flowable:type="external"` service task in the BPMN fixture becomes an
*external job*. Unlike Camunda/Operaton, Flowable's acquire endpoint locks jobs
of a SINGLE topic per call (workerId + topic + lockDuration). Jobs are created
with a default of 3 retries; a worker failure reports a technical failure and
the engine re-activates the job after the retry timeout (lock expiry reset).

Business logic is intentionally kept here, never inside the engine.
"""

import uuid

from app.workflow.flowable import FlowableAdapter

TOPIC_DEFAULT = "load_prisoner_data"


class FlowableExternalTaskWorker:
    """Polls one external-job topic and completes the jobs.

    fail_first mode simulates an unreliable external service: the first job
    handed to this process reports a technical failure with retries remaining,
    so Flowable's retry mechanism re-activates the job and the next attempt
    succeeds.
    """

    def __init__(
        self,
        engine_url: str | None = None,
        worker_id: str | None = None,
        topic: str = TOPIC_DEFAULT,
        lock_duration_ms: int = 30000,
        fail_first: bool = False,
    ) -> None:
        self.worker_id = worker_id or f"benchmark-flowable-worker-{uuid.uuid4().hex[:8]}"
        self.topic = topic
        self.lock_duration_ms = lock_duration_ms
        self.fail_first = fail_first
        self._failed_once = False
        self.results = {"fetched": 0, "completed": 0, "failed": 0}
        self._adapter = FlowableAdapter(base_url=engine_url)

    def fetch_and_lock(self, max_tasks: int = 5) -> list[dict]:
        return self._adapter.fetch_external_jobs(
            topic=self.topic,
            worker_id=self.worker_id,
            max_tasks=max_tasks,
            lock_duration_ms=self.lock_duration_ms,
        )

    def complete(self, job_id: str, variables: dict | None = None) -> None:
        self._adapter.complete_external_job(job_id, self.worker_id, variables=variables)

    def fail(
        self,
        job_id: str,
        error_message: str,
        error_details: str | None = None,
        retries: int | None = None,
        retry_timeout_ms: int | None = None,
    ) -> None:
        self._adapter.fail_external_job(
            job_id,
            self.worker_id,
            error_message=error_message,
            error_details=error_details,
            retries=retries,
            retry_timeout_ms=retry_timeout_ms,
        )

    # ---- orchestration -----------------------------------------------------

    def poll_once(self, handle=None, max_tasks: int = 5, instance_filter: str | None = None) -> int:
        """Process one batch of external jobs.

        handle(job: dict) -> dict | None: returns variables to complete the
        job with, or None to report a failure. Defaults to the standard
        prisoner-data payload. If instance_filter is set, only jobs belonging
        to that process instance are handled.
        """
        jobs = self.fetch_and_lock(max_tasks=max_tasks)
        processed = 0
        for job in jobs:
            if instance_filter and job.get("processInstanceId") != instance_filter:
                # leave other instances' jobs alone; unlock happens via lock expiry
                continue
            job_id = job["id"]
            self.results["fetched"] += 1

            if self.fail_first and not self._failed_once:
                self._failed_once = True
                self.fail(
                    job_id,
                    error_message="simulated technical failure",
                    error_details="first execution fails by design (worker fail-first test mode)",
                    retries=2,
                    retry_timeout_ms=3000,
                )
                self.results["failed"] += 1
                processed += 1
                continue

            variables = handle(job) if handle else default_prisoner_payload(job)
            if variables is None:
                self.fail(
                    job_id,
                    error_message="external service returned no data",
                    retries=2,
                    retry_timeout_ms=5000,
                )
                self.results["failed"] += 1
            else:
                self.complete(job_id, variables)
                self.results["completed"] += 1
            processed += 1
        return processed

    def close(self) -> None:
        self._adapter.close()


def default_prisoner_payload(job: dict) -> dict:
    """Simulated external service response for the prisoner-data topic."""
    return {"prisoner_exists": True, "unit": 3}
