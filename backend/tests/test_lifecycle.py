"""Unit tests for app/lifecycle.py: every status transition, role and scope rule, verification
rules (docs/api-contract.md §6) and work-order rules, with no database or HTTP."""

import itertools

import pytest

from app import lifecycle as lc
from app.lifecycle import RuleViolation

OFFICIAL = {"pk": 1, "role": "official", "departmentPks": [10]}
OTHER_OFFICIAL = {"pk": 2, "role": "official", "departmentPks": [20]}
SUPERVISOR = {"pk": 3, "role": "supervisor", "departmentPks": []}
ADMIN = {"pk": 4, "role": "admin", "departmentPks": []}


def case(status="Submitted", verification="Pending Verification", department=10, assignee=None):
    return {"status": status, "verification": verification, "departmentPk": department, "assignedStaffPk": assignee}


def violation(fn, *args, **kwargs) -> RuleViolation:
    with pytest.raises(RuleViolation) as err:
        fn(*args, **kwargs)
    return err.value


def test_every_status_pair_is_either_listed_or_refused():
    for current, to in itertools.product(lc.STATUSES, lc.STATUSES):
        c = case(current)
        if (current, to) in lc.STATUS_TRANSITIONS:
            lc.check_status_transition(c, ADMIN, to, explanation="because")
        else:
            assert violation(lc.check_status_transition, c, ADMIN, to, "because").code == "invalid_transition"


def test_unknown_status_is_a_validation_error():
    assert violation(lc.check_status_transition, case(), ADMIN, "Closed").status == 422


@pytest.mark.parametrize("current,to", [("Assigned", "Under Review"), ("Marked Resolved", "In Progress")])
def test_supervisor_only_transitions(current, to):
    assert violation(lc.check_status_transition, case(current), OFFICIAL, to, "x").status == 403
    lc.check_status_transition(case(current), SUPERVISOR, to, "x")


@pytest.mark.parametrize("current,to", [("Assigned", "Under Review"), ("In Progress", "Assigned"),
                                        ("In Progress", "Marked Resolved"), ("Marked Resolved", "In Progress")])
def test_explanation_required(current, to):
    assert violation(lc.check_status_transition, case(current), ADMIN, to, "  ").code == "explanation_required"


def test_assigned_needs_a_department_and_resolution_needs_no_open_work():
    assert violation(lc.check_status_transition, case("Under Review", department=None), ADMIN, "Assigned").code == "department_required"
    assert violation(lc.check_status_transition, case("In Progress"), OFFICIAL, "Marked Resolved", "done",
                     active_work_orders=1).code == "work_orders_open"
    lc.check_status_transition(case("In Progress"), OFFICIAL, "Marked Resolved", "done", active_work_orders=0)


def test_official_scope():
    assert lc.in_scope(case(department=10), OFFICIAL)
    assert not lc.in_scope(case(department=10), OTHER_OFFICIAL)
    assert lc.in_scope(case(department=10, assignee=2), OTHER_OFFICIAL), "own assignment"
    assert lc.in_scope(case(department=None), OTHER_OFFICIAL), "unassigned triage queue"
    assert lc.in_scope(case(department=99), SUPERVISOR) and lc.in_scope(case(department=99), ADMIN)
    assert violation(lc.check_status_transition, case("Submitted"), OTHER_OFFICIAL, "Under Review").status == 403
    assert lc.allowed_actions(case(department=10), OTHER_OFFICIAL) == []


def test_unknown_role_has_no_rights():
    nobody = {"pk": 9, "role": "citizen", "departmentPks": [10]}
    assert not lc.has_role(nobody, "official")
    assert violation(lc.check_status_transition, case(), nobody, "Under Review").status == 403


def test_expected_state_conflict():
    lc.require_expected("Assigned", "Assigned")
    lc.require_expected("Assigned", None)
    assert violation(lc.require_expected, "In Progress", "Assigned").status == 409


def test_resolving_again_opens_a_new_verification_round():
    assert lc.verification_after_status(case(verification="Disputed"), "Marked Resolved") == "Pending Verification"
    assert lc.verification_after_status(case(verification="Reinspection Required"), "Marked Resolved") == "Pending Verification"
    assert lc.verification_after_status(case(verification="Pending Verification"), "Marked Resolved") is None
    assert lc.verification_after_status(case(verification="Disputed"), "In Progress") is None


@pytest.mark.parametrize("status,verification,response,expected", [
    ("Marked Resolved", "Pending Verification", "Appears Resolved", "Citizen Confirmed"),
    ("Marked Resolved", "Disputed", "Appears Resolved", "Citizen Confirmed"),
    ("Marked Resolved", "Citizen Confirmed", "Appears Resolved", None),
    ("Marked Resolved", "Reinspection Required", "Appears Resolved", None),
    ("Marked Resolved", "Pending Verification", "Still Present", "Disputed"),
    ("Marked Resolved", "Citizen Confirmed", "Still Present", "Disputed"),
    ("Marked Resolved", "Reinspection Required", "Still Present", None),
    ("In Progress", "Pending Verification", "Still Present", None),
    ("Marked Resolved", "Pending Verification", "Cannot Confirm", None),
    ("Submitted", "Pending Verification", "Cannot Confirm", None),
])
def test_citizen_verification_rules(status, verification, response, expected):
    assert lc.citizen_verification(case(status, verification), response) == expected


def test_confirmation_before_resolution_and_unknown_responses_are_refused():
    assert violation(lc.citizen_verification, case("In Progress"), "Appears Resolved").code == "not_resolved"
    assert violation(lc.citizen_verification, case("Marked Resolved"), "Fixed!").status == 422


def test_citizen_responses_never_change_status():
    # citizen_verification returns only a verification value; there is no status output at all.
    for status, response in itertools.product(lc.STATUSES, lc.CITIZEN_RESPONSES):
        try:
            result = lc.citizen_verification(case(status), response)
        except RuleViolation:
            continue
        assert result is None or result in lc.VERIFICATIONS


def test_assignment_rules():
    dept = {"pk": 10, "active": True}
    member = {"pk": 1, "role": "official", "departmentPks": [10], "disabledAt": None}
    outsider = {"pk": 2, "role": "official", "departmentPks": [20], "disabledAt": None}
    assert violation(lc.check_assignment, case(), OFFICIAL, dept, None).status == 403
    lc.check_assignment(case(), SUPERVISOR, dept, member)
    lc.check_assignment(case(), SUPERVISOR, dept, {"pk": 3, "role": "supervisor", "departmentPks": [], "disabledAt": None})
    assert violation(lc.check_assignment, case(), SUPERVISOR, dept, outsider).code == "invalid_assignee"
    assert violation(lc.check_assignment, case(), SUPERVISOR, dept, member | {"disabledAt": "z"}).code == "invalid_assignee"
    assert violation(lc.check_assignment, case(), SUPERVISOR, {"pk": 10, "active": False}, None).code == "invalid_department"
    assert violation(lc.check_assignment, case(), SUPERVISOR, None, None).code == "invalid_department"
    assert violation(lc.check_assignment, case("Marked Resolved"), ADMIN, dept, None).status == 409
    assert lc.status_after_assignment(case("Submitted")) == "Assigned"
    assert lc.status_after_assignment(case("Under Review")) == "Assigned"
    assert lc.status_after_assignment(case("In Progress")) is None


def test_work_order_rules():
    order = {"status": "Open"}
    assert violation(lc.check_work_order_create, case("Under Review"), OFFICIAL).status == 409
    lc.check_work_order_create(case("Assigned"), OFFICIAL)
    assert violation(lc.check_work_order_create, case("Assigned"), OTHER_OFFICIAL).status == 403
    lc.check_work_order_change(case("Assigned"), order, OFFICIAL, "In Progress")
    assert violation(lc.check_work_order_change, case("Assigned"), order, OFFICIAL, "Completed", "x").code == "invalid_transition"
    assert violation(lc.check_work_order_change, case("Assigned"), {"status": "In Progress"}, OFFICIAL, "Completed", "").code == "note_required"
    assert violation(lc.check_work_order_change, case("Assigned"), order, OFFICIAL, "Cancelled", "dup").status == 403
    lc.check_work_order_change(case("Assigned"), order, SUPERVISOR, "Cancelled", "dup")
    for terminal in ("Completed", "Cancelled"):
        for to in lc.WORK_ORDER_STATUSES:
            assert violation(lc.check_work_order_change, case(), {"status": terminal}, ADMIN, to, "x").status in (409,)
    assert lc.status_after_work_started(case("Assigned")) == "In Progress"
    assert lc.status_after_work_started(case("In Progress")) is None


def test_reinspection_rules():
    resolved = case("Marked Resolved", "Disputed")
    assert violation(lc.check_reinspection, resolved, OFFICIAL, "x").status == 403
    assert violation(lc.check_reinspection, case("In Progress"), SUPERVISOR, "x").status == 409
    assert violation(lc.check_reinspection, case("Marked Resolved", "Reinspection Required"), SUPERVISOR, "x").status == 409
    assert violation(lc.check_reinspection, resolved, SUPERVISOR, " ").code == "note_required"
    lc.check_reinspection(resolved, SUPERVISOR, "Pothole reopened after rain")


def test_allowed_actions_match_the_checks():
    """Every transition offered by allowed_actions passes check_status_transition, and vice versa."""
    for status, staff, dept, wo in itertools.product(lc.STATUSES, (OFFICIAL, SUPERVISOR, ADMIN), (10, None), (0, 1)):
        c = case(status, department=dept)
        offered = {a["to"] for a in lc.allowed_actions(c, staff, wo) if a["action"] == "transition"}
        for to in lc.STATUSES:
            try:
                lc.check_status_transition(c, staff, to, "because", wo)
                ok = True
            except RuleViolation:
                ok = False
            assert ok == (to in offered), (status, staff["role"], dept, wo, to)
    assert "assign" not in {a["action"] for a in lc.allowed_actions(case(), OFFICIAL)}
    assert "assign" in {a["action"] for a in lc.allowed_actions(case(), SUPERVISOR)}
