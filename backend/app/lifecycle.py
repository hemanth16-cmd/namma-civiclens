"""Complaint-management rules: the operational lifecycle, citizen verification, work orders and who
may do what. Pure functions with no SQL or HTTP, so the web API, the tests and any other channel
(for example a Telegram bot) apply exactly the same rules. store.py calls these inside the
transaction that holds the complaint row, so a decision is always made on current data.

Two independent state machines (docs/CORE_PLATFORM_PLAN.md §5.1):
- status, the operational lifecycle, is changed only by staff;
- verification, the citizen's view of a resolution, is changed by citizen responses (with the claim
  code) and by a supervisor's reinspection request, never by an ordinary status change, except that
  marking a case resolved again opens a fresh verification round.

Roles: official < supervisor < admin. An official acts only on complaints in one of their departments,
assigned to them, or not yet assigned to any department (the triage queue).
"""

from dataclasses import dataclass

STATUSES = ("Submitted", "Under Review", "Assigned", "In Progress", "Under Construction", "Work Completed", "Verification In Progress", "On Hold", "Marked Resolved")
VERIFICATIONS = ("Pending Verification", "Citizen Confirmed", "Disputed", "Reinspection Required")
CITIZEN_RESPONSES = ("Appears Resolved", "Still Present", "Cannot Confirm")
WORK_ORDER_STATUSES = ("Open", "In Progress", "Completed", "Cancelled")
ACTIVE_WORK_ORDER = ("Open", "In Progress")
ROLES = ("official", "supervisor", "admin")
_RANK = {role: i for i, role in enumerate(ROLES)}


@dataclass(frozen=True)
class Transition:
    min_role: str
    needs_explanation: bool = False   # a public explanation is required
    label: str = ""


# (from, to) → rule. Anything not listed is refused.
STATUS_TRANSITIONS: dict[tuple[str, str], Transition] = {
    ("Submitted", "Under Review"): Transition("official", label="Start review"),
    ("Under Review", "Assigned"): Transition("official", label="Mark assigned"),          # needs a department
    ("Assigned", "Under Review"): Transition("supervisor", True, "Return to review"),
    ("Assigned", "In Progress"): Transition("official", label="Start work"),
    ("In Progress", "Assigned"): Transition("official", True, "Pause work"),
    ("In Progress", "Marked Resolved"): Transition("official", True, "Mark resolved"),    # no active work orders
    ("Marked Resolved", "In Progress"): Transition("supervisor", True, "Reopen"),
}


# Additional progress stages retain the existing final status and independent citizen verification.
# Officials can advance operational work; supervisors can correct an earlier stage or reopen a case.
for source in ("Assigned", "In Progress", "Under Construction", "Work Completed", "Verification In Progress", "On Hold"):
    for target, label in (("In Progress", "Resume work"), ("Under Construction", "Construction underway"),
                          ("Work Completed", "Work finished; awaiting checks"),
                          ("Verification In Progress", "Inspect completed work"), ("On Hold", "Put work on hold")):
        if source != target:
            STATUS_TRANSITIONS.setdefault((source, target), Transition("official", True, label))
    if source != "Assigned":
        STATUS_TRANSITIONS.setdefault((source, "Marked Resolved"), Transition("official", True, "Completed / mark resolved"))
# Administrators can correct a stage directly, with a public reason.
for source in STATUSES:
    for target in STATUSES:
        if source != target:
            STATUS_TRANSITIONS.setdefault((source, target), Transition("admin", True, "Update stage"))


class RuleViolation(Exception):
    """An action is not allowed. `status` is the HTTP status the API returns."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def has_role(staff: dict, minimum: str) -> bool:
    return _RANK.get(staff.get("role"), -1) >= _RANK[minimum]


def require_role(staff: dict, minimum: str, what: str) -> None:
    if not has_role(staff, minimum):
        raise RuleViolation(403, "forbidden", f"Only a {minimum} or above can {what}.")


def in_scope(case: dict, staff: dict) -> bool:
    """Supervisors and admins see every case. An official: their departments, their own assignments,
    or the unassigned triage queue."""
    if has_role(staff, "supervisor"):
        return True
    if case.get("assignedStaffPk") is not None and case["assignedStaffPk"] == staff["pk"]:
        return True
    if case.get("departmentPk") is None:
        return True
    return case["departmentPk"] in set(staff.get("departmentPks") or ())


def require_scope(case: dict, staff: dict) -> None:
    if not in_scope(case, staff):
        raise RuleViolation(403, "forbidden", "This complaint belongs to a department you are not a member of.")


def require_expected(current: str, expected: str | None, what: str = "status") -> None:
    """Optimistic concurrency: the client says which state it saw; if the case moved on, refuse."""
    if expected is not None and expected != current:
        raise RuleViolation(409, "conflict", f"The {what} changed to \"{current}\" since you loaded it. Reload and try again.")


def require_not_merged(case: dict) -> None:
    """A complaint merged into another as a duplicate is managed through its canonical complaint."""
    if case.get("mergedIntoPk") is not None:
        raise RuleViolation(409, "merged", "This complaint was merged into another as a duplicate. Act on that complaint, or revert the merge first.")


def check_status_transition(case: dict, staff: dict, to: str, explanation: str = "", active_work_orders: int = 0) -> Transition:
    """Validates staff moving `case` to `to`. Returns the rule, or raises RuleViolation."""
    if to not in STATUSES:
        raise RuleViolation(422, "invalid_status", "Choose a valid lifecycle status.")
    current = case["status"]
    rule = STATUS_TRANSITIONS.get((current, to))
    if rule is None:
        raise RuleViolation(409, "invalid_transition", f"A complaint cannot move from \"{current}\" to \"{to}\".")
    require_role(staff, rule.min_role, f"move a complaint from \"{current}\" to \"{to}\"")
    require_scope(case, staff)
    require_not_merged(case)
    if rule.needs_explanation and not explanation.strip():
        raise RuleViolation(422, "explanation_required", f"Explain publicly why the complaint moves to \"{to}\".")
    if to == "Assigned" and case.get("departmentPk") is None:
        raise RuleViolation(409, "department_required", "Assign a department before marking the complaint assigned.")
    if to == "Marked Resolved" and active_work_orders:
        raise RuleViolation(409, "work_orders_open", "Complete or cancel the open work orders before marking the complaint resolved.")
    return rule


def verification_after_status(case: dict, to: str) -> str | None:
    """Marking a case resolved (again) opens a new verification round after a dispute or reinspection.
    Returns the new verification, or None when it does not change."""
    if to == "Marked Resolved" and case["verification"] in ("Disputed", "Reinspection Required"):
        return "Pending Verification"
    return None


def check_assignment(case: dict, staff: dict, department: dict | None, assignee: dict | None) -> None:
    """Supervisor+ assigns or reassigns a department and optionally a staff member."""
    require_role(staff, "supervisor", "assign or reassign complaints")
    require_not_merged(case)
    if case["status"] == "Marked Resolved":
        raise RuleViolation(409, "invalid_transition", "Reopen the complaint before reassigning it.")
    if department is None or not department.get("active"):
        raise RuleViolation(422, "invalid_department", "Choose an active department.")
    if assignee is not None:
        if assignee.get("disabledAt"):
            raise RuleViolation(422, "invalid_assignee", "That staff account is disabled.")
        if assignee["role"] == "official" and department["pk"] not in set(assignee.get("departmentPks") or ()):
            raise RuleViolation(422, "invalid_assignee", "That official is not a member of the chosen department.")


def status_after_assignment(case: dict) -> str | None:
    return "Assigned" if case["status"] in ("Submitted", "Under Review") else None


def check_note(case: dict, staff: dict) -> None:
    require_scope(case, staff)


def check_work_order_create(case: dict, staff: dict) -> None:
    require_scope(case, staff)
    require_not_merged(case)
    if case["status"] not in ("Assigned", "In Progress", "Under Construction", "Work Completed", "Verification In Progress", "On Hold"):
        raise RuleViolation(409, "invalid_transition", "Assign the complaint before creating a work order.")


def check_work_order_change(case: dict, order: dict, staff: dict, to: str, note: str = "") -> None:
    """Open → In Progress (start); In Progress → Completed (needs a completion note);
    Open or In Progress → Cancelled (supervisor+, needs a reason)."""
    require_scope(case, staff)
    current = order["status"]
    allowed = {("Open", "In Progress"), ("In Progress", "Completed"), ("Open", "Cancelled"), ("In Progress", "Cancelled")}
    if to not in WORK_ORDER_STATUSES:
        raise RuleViolation(422, "invalid_status", "Choose a valid work-order status.")
    if (current, to) not in allowed:
        raise RuleViolation(409, "invalid_transition", f"A work order cannot move from \"{current}\" to \"{to}\".")
    if to == "Cancelled":
        require_role(staff, "supervisor", "cancel work orders")
    if to in ("Completed", "Cancelled") and not note.strip():
        raise RuleViolation(422, "note_required", "Add a completion note." if to == "Completed" else "Give a reason for cancelling.")


def status_after_work_started(case: dict) -> str | None:
    """Starting the first work order on an assigned complaint puts the complaint in progress."""
    return "In Progress" if case["status"] == "Assigned" else None


def check_reinspection(case: dict, staff: dict, note: str) -> None:
    require_role(staff, "supervisor", "request a reinspection")
    require_not_merged(case)
    if case["status"] != "Marked Resolved":
        raise RuleViolation(409, "invalid_transition", "Reinspection applies only to complaints marked resolved.")
    if case["verification"] == "Reinspection Required":
        raise RuleViolation(409, "invalid_transition", "A reinspection is already required.")
    if not note.strip():
        raise RuleViolation(422, "note_required", "Explain why a reinspection is needed.")


def citizen_verification(case: dict, response: str) -> str | None:
    """The verification after a citizen response, or None when it is logged without a change
    (docs/api-contract.md §6). Raises for a confirmation before the case is marked resolved."""
    if response not in CITIZEN_RESPONSES:
        raise RuleViolation(422, "invalid_response", "Choose a valid response.")
    resolved = case["status"] == "Marked Resolved"
    if response == "Appears Resolved":
        if not resolved:
            raise RuleViolation(409, "not_resolved", "Confirmation opens after the civic team marks this issue resolved.")
        return "Citizen Confirmed" if case["verification"] in ("Pending Verification", "Disputed") else None
    if response == "Still Present" and resolved and case["verification"] in ("Pending Verification", "Citizen Confirmed"):
        return "Disputed"
    return None


def check_merge(duplicate: dict, canonical: dict, staff: dict, duplicate_has_duplicates: bool, reason: str) -> None:
    """Supervisor+ merges `duplicate` into `canonical`. No self-merge, no chains (the canonical must not
    itself be merged, and a complaint that others were merged into cannot be merged away), no double merge."""
    require_role(staff, "supervisor", "merge duplicate complaints")
    if duplicate["pk"] == canonical["pk"]:
        raise RuleViolation(422, "invalid_merge", "A complaint cannot be merged into itself.")
    if duplicate.get("mergedIntoPk") is not None:
        raise RuleViolation(409, "already_merged", "This complaint is already merged into another. Revert that merge first.")
    if canonical.get("mergedIntoPk") is not None:
        raise RuleViolation(409, "canonical_merged", "The chosen complaint is itself a merged duplicate. Merge into its canonical complaint instead.")
    if duplicate_has_duplicates:
        raise RuleViolation(409, "has_duplicates", "Other reports were merged into this complaint. Revert those merges, or merge them into the new canonical complaint, first.")
    if not reason.strip():
        raise RuleViolation(422, "reason_required", "Explain why these reports describe the same issue.")


def check_merge_revert(staff: dict, reason: str) -> None:
    require_role(staff, "supervisor", "revert merges")
    if not reason.strip():
        raise RuleViolation(422, "reason_required", "Give a reason for reverting the merge.")


def allowed_actions(case: dict, staff: dict, active_work_orders: int = 0) -> list[dict]:
    """What this staff member may do now. The console shows only these; the server re-checks every
    request anyway, so this list is a convenience and never the authority."""
    if not in_scope(case, staff):
        return []
    if case.get("mergedIntoPk") is not None:  # a merged duplicate: notes only, plus reverting for supervisors
        merged = [{"action": "add_note", "label": "Add internal note"}]
        if has_role(staff, "supervisor"):
            merged.append({"action": "revert_merge", "label": "Revert merge"})
        return merged
    actions = []
    for (current, to), rule in STATUS_TRANSITIONS.items():
        if current != case["status"] or not has_role(staff, rule.min_role):
            continue
        if to == "Assigned" and case.get("departmentPk") is None:
            continue
        if to == "Marked Resolved" and active_work_orders:
            continue
        actions.append({"action": "transition", "to": to, "label": rule.label, "needsExplanation": rule.needs_explanation})
    if has_role(staff, "supervisor") and case["status"] != "Marked Resolved":
        actions.append({"action": "assign", "label": "Assign or reassign"})
    if case["status"] in ("Assigned", "In Progress", "Under Construction", "Work Completed", "Verification In Progress", "On Hold"):
        actions.append({"action": "create_work_order", "label": "Create work order"})
    if has_role(staff, "supervisor") and case["status"] == "Marked Resolved" and case["verification"] != "Reinspection Required":
        actions.append({"action": "request_reinspection", "label": "Request reinspection"})
    actions.append({"action": "review_duplicates", "label": "Review possible duplicates"})
    if has_role(staff, "supervisor"):
        actions.append({"action": "merge", "label": "Merge into another complaint"})
    actions.append({"action": "add_note", "label": "Add internal note"})
    actions.append({"action": "add_public_update", "label": "Post public update"})
    return actions
