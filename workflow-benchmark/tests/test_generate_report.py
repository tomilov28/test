"""Unit tests for scripts/generate_report.py hardening (never runs main()).

Covers the strict evidence checks: suite PASS requires present + 0
failures/errors/skips; a specific test PASS requires the exact testcase name;
R1-R5/stress PASS requires the scenario JSON to exist and parse; evidence
confidence is HIGH only when ALL mandatory evidence is present and valid; the
expert matrix totals are 97/91 with the resource delta computed from measured
median RSS.
"""

import json

from scripts.generate_report import (
    SCENARIO_FILES,
    evidence_conf,
    evidence_files_contain,
    rate,
    scenario_ok,
    suite_present_pass,
    suite_status,
    test_passed as check_test_passed,
)

OP_RSS = 335229747  # Operaton median idle RSS from the authoritative run (~319.7 MiB)
FL_RSS = 282480000  # Flowable median idle RSS (~269.4 MiB)


def _suite(present=True, failures=0, errors=0, skipped=0, testcases=()):
    return {
        "present": present,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "testcases": list(testcases),
    }


def _engine(key, files):
    return {
        "functional_junit": _suite(
            testcases=["tests/test_x.py::test_t02_start_outbox_creates_instance"]
        ),
        "fault_junit": _suite(),
        "audit_junit": _suite(),
        "resource_metrics": {"summary": {"median_rss_bytes": OP_RSS if key == "operaton" else FL_RSS}},
        "evidence_files": files,
    }


def _data():
    base = [
        "api-evidence/engine_info.json",
        "api-evidence/deployments.json",
        "api-evidence/process-definitions.json",
        "api-evidence/running-instances.json",
        "operational/demo.json",
        "operational/incident.json",
        "engine.log",
        "app.log",
    ]
    op_files = base + [
        "api-evidence/t07-full-restart.json",
        "api-evidence/t09-durable-timer-restart.json",
    ]
    fl_files = [f.replace("t07", "f07").replace("t09", "f09") for f in op_files]
    return {
        "unit": _suite(testcases=["tests/test_completion.py::test_approve_maps_to_completed"]),
        "operaton": _engine("operaton", op_files),
        "flowable": _engine("flowable", fl_files),
    }


def _write_scenarios(tmp_path, key):
    d = tmp_path / key / "fault-scenarios"
    d.mkdir(parents=True, exist_ok=True)
    for scen_id, fname in SCENARIO_FILES.items():
        body = {"scenario": scen_id}
        if scen_id == "stress":
            body.update(
                {
                    "instances_unique_per_request": True,
                    "no_failures": {"failed_commands": 0, "engine_failed_jobs": 0},
                }
            )
        (d / fname).write_text(json.dumps(body))


def _write_all_scenarios(tmp_path):
    _write_scenarios(tmp_path, "operaton")
    _write_scenarios(tmp_path, "flowable")


def test_suite_status_requires_present_and_clean():
    assert suite_status(_suite(present=True), "s") == "PASS"
    assert suite_status(_suite(present=False), "s") == "BLOCKED"
    assert suite_status(_suite(failures=1), "s") == "FAIL"
    assert suite_status(_suite(errors=1), "s") == "FAIL"
    assert suite_status(_suite(skipped=1), "s") == "FAIL"
    assert suite_present_pass(_suite(present=True)) is True
    assert suite_present_pass(_suite(skipped=2)) is False


def test_test_passed_exact_leaf_name_only():
    suite = _suite(testcases=["tests/test_x.py::test_approve_maps_to_completed", "other::test_foo"])
    assert check_test_passed(suite, "test_approve_maps_to_completed") is True
    assert check_test_passed(suite, "test_foo") is True
    assert check_test_passed(suite, "test_approve") is False  # substring is not enough
    assert check_test_passed(suite, "test_approve_maps_to_completed2") is False
    assert check_test_passed(_suite(testcases=[]), "test_foo") is False


def test_scenario_ok_requires_file_and_parse(tmp_path):
    assert scenario_ok(str(tmp_path), "operaton", "r1") is False  # missing file
    d = tmp_path / "operaton" / "fault-scenarios"
    d.mkdir(parents=True)
    (d / "r1-restart-during-human-task.json").write_text("not json{")
    assert scenario_ok(str(tmp_path), "operaton", "r1") is False  # unparseable
    (d / "r1-restart-during-human-task.json").write_text(json.dumps({"scenario": "r2"}))
    assert scenario_ok(str(tmp_path), "operaton", "r1") is False  # wrong scenario id
    (d / "r1-restart-during-human-task.json").write_text(json.dumps({"scenario": "r1"}))
    assert scenario_ok(str(tmp_path), "operaton", "r1") is True


def test_stress_scenario_requires_pass_criteria(tmp_path):
    d = tmp_path / "flowable" / "fault-scenarios"
    d.mkdir(parents=True)
    fname = SCENARIO_FILES["stress"]
    (d / fname).write_text(json.dumps({"scenario": "stress"}))
    assert scenario_ok(str(tmp_path), "flowable", "stress") is False  # no criteria
    (d / fname).write_text(
        json.dumps(
            {
                "scenario": "stress",
                "instances_unique_per_request": True,
                "no_failures": {"failed_commands": 1, "engine_failed_jobs": 0},
            }
        )
    )
    assert scenario_ok(str(tmp_path), "flowable", "stress") is False  # failed command
    (d / fname).write_text(
        json.dumps(
            {
                "scenario": "stress",
                "instances_unique_per_request": True,
                "no_failures": {"failed_commands": 0, "engine_failed_jobs": 0},
            }
        )
    )
    assert scenario_ok(str(tmp_path), "flowable", "stress") is True


def test_evidence_files_contain_is_concrete():
    data = _data()
    assert evidence_files_contain(data, "operaton", "api-evidence/t07-full-restart.json") is True
    assert evidence_files_contain(data, "operaton", "f07-full-restart.json") is False
    assert evidence_files_contain(data, "flowable", "api-evidence/f07-full-restart.json") is True
    assert evidence_files_contain(data, "flowable", "t07-full-restart.json") is False


def test_evidence_conf_high_only_with_all_mandatory_evidence(tmp_path):
    _write_all_scenarios(tmp_path)
    data = _data()
    assert evidence_conf(data, str(tmp_path)) == "HIGH"

    # a missing scenario file drops confidence to MEDIUM (1 gap)
    (tmp_path / "operaton" / "fault-scenarios" / SCENARIO_FILES["r5"]).unlink()
    assert evidence_conf(data, str(tmp_path)) == "MEDIUM"

    # a dirty suite is a gap too
    data["flowable"]["fault_junit"]["failures"] = 1
    assert evidence_conf(data, str(tmp_path)) == "MEDIUM"

    # 3+ gaps -> LOW
    data["operaton"]["audit_junit"]["present"] = False
    data["flowable"]["resource_metrics"] = {}
    (tmp_path / "flowable" / "fault-scenarios" / SCENARIO_FILES["stress"]).unlink()
    assert evidence_conf(data, str(tmp_path)) == "LOW"


def test_rate_totals_and_resource_delta_from_measured_rss():
    data = _data()
    scores = rate(data)
    assert scores["operaton"]["total"] == 97
    assert scores["flowable"]["total"] == 91
    # resource row is fed by the measured median RSS, not a hardcoded number
    assert scores["operaton"]["median_rss_mib"] == round(OP_RSS / 1024 / 1024, 1)
    assert scores["flowable"]["median_rss_mib"] == round(FL_RSS / 1024 / 1024, 1)
    assert scores["operaton"]["scores"]["Resource footprint"]["score"] == 3
    assert scores["flowable"]["scores"]["Resource footprint"]["score"] == 5
    assert "Resource footprint" in scores["operaton"]["scores"]
