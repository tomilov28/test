"""Unit tests for the CompletionContract (audit A02)."""

import pytest

from app.domain.completion import DECISION_OUTCOMES, FINAL_TASK_KEY, validate_completion_contract


def test_non_final_task_has_no_contract():
    assert validate_completion_contract("finance_check", {}) is None
    assert validate_completion_contract("finance_check", {"result": "OK"}) is None


def test_approve_maps_to_completed():
    decision = validate_completion_contract(FINAL_TASK_KEY, {"decision": "APPROVE"})
    assert decision == "APPROVE"
    assert DECISION_OUTCOMES[decision] == "COMPLETED"


def test_reject_maps_to_rejected():
    decision = validate_completion_contract(FINAL_TASK_KEY, {"decision": "REJECT"})
    assert decision == "REJECT"
    assert DECISION_OUTCOMES[decision] == "REJECTED"


@pytest.mark.parametrize("bad", [{}, {"decision": None}, {"decision": "MAYBE"}, {"decision": ""}])
def test_invalid_decision_raises(bad):
    with pytest.raises(ValueError):
        validate_completion_contract(FINAL_TASK_KEY, bad)
