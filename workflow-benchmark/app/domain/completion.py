"""CompletionContract for the LONG_VISIT_POC domain (audit A02).

The workflow engine's END state is a TECHNICAL state and never assigns a
business outcome. The only way a Request acquires a business outcome is a final
domain action (the `final_decision` human task) or an explicit user
cancellation. This module encodes the minimal contract for the final action:

    decision=APPROVE -> Request CLOSED/COMPLETED
    decision=REJECT  -> Request CLOSED/REJECTED

The final domain action is applied in ONE transaction:
    validate CompletionContract
    save TaskResult
    WorkItem -> COMPLETED
    Request  -> CLOSED + outcome
    enqueue COMPLETE_TASK (technical convergence on the engine)
    COMMIT
"""

FINAL_TASK_KEY = "final_decision"

VALID_DECISIONS = ("APPROVE", "REJECT")

DECISION_OUTCOMES = {
    "APPROVE": "COMPLETED",
    "REJECT": "REJECTED",
}


def validate_completion_contract(task_definition_key: str, data: dict | None) -> str | None:
    """Validate the CompletionContract for a work item's final domain action.

    Returns the decision for the final task, or None when the task is not a
    contract-bearing final task. Raises ValueError when the contract is
    violated (e.g. a final task with a missing/invalid decision); the Request
    and WorkItem must then stay untouched.
    """
    if task_definition_key != FINAL_TASK_KEY:
        return None
    decision = (data or {}).get("decision")
    if decision not in VALID_DECISIONS:
        raise ValueError(
            f"completion contract violation: final_decision requires decision in "
            f"{sorted(VALID_DECISIONS)}, got {decision!r}"
        )
    return decision
