from .domain import DomainError, require_text

OCCUPANCY_ROLES = {"dispatcher", "coordinator"}
RECEIPT_ROLES = {"field_operator", "dispatcher"}


def normalize_occupancy(payload):
    branch_id = require_text(payload, "branch_id")
    event_id = payload.get("event_id")
    if isinstance(event_id, bool):
        raise DomainError("invalid_event_id", "event_id 必须是整数")
    try:
        event_id = int(event_id)
    except (TypeError, ValueError):
        raise DomainError("invalid_event_id", "event_id 必须是整数")

    valves = payload.get("valve_ids", [])
    if not isinstance(valves, list) or not valves:
        raise DomainError("valves_required", "至少需要一个隔离阀门")
    normalized_valves = []
    for valve in valves:
        if not isinstance(valve, str) or not valve.strip():
            raise DomainError("invalid_valve", "阀门编号必须是字符串")
        valve = valve.strip()
        if valve in normalized_valves:
            raise DomainError("duplicate_valve", "同一阀门不能在凭证中重复")
        normalized_valves.append(valve)

    return {
        "branch_id": branch_id.strip(),
        "event_id": event_id,
        "valve_ids": normalized_valves,
        "reason": str(payload.get("reason", "")).strip(),
    }


def normalize_valve_receipt(payload):
    branch_id = require_text(payload, "branch_id")
    valve_id = require_text(payload, "valve_id")
    command_no = require_text(payload, "command_no")
    closed = payload.get("closed")
    if not isinstance(closed, bool):
        raise DomainError("invalid_closed", "closed 必须是布尔值")
    return {
        "branch_id": branch_id.strip(),
        "valve_id": valve_id.strip(),
        "command_no": command_no.strip(),
        "closed": closed,
        "note": str(payload.get("note", "")).strip(),
    }
