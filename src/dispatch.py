"""管网调度账：事件、支路隔离凭证、关阀回执。

一条支路同时可以被多个未结束事件占用，每个事件对每条支路只有一张
有效占用单；同一事件重复申请占用时拿回原单。关阀按命令号重试，已
确认的阀门保持不变。所有阀门确认关断后才能冲洗；恢复时只要还有别的
未结束事件占着该支路，就拦住开阀并列出占用单号。
"""

from .domain import DomainError

ENTITY_TYPE = "network_event"
INITIAL_STATUS = "open"

EVENT_CREATE_ROLES = {"dispatcher", "coordinator"}
BRANCH_ROLES = {"dispatcher", "coordinator"}
OCCUPATION_ROLES = {"dispatcher", "coordinator", "field_operator"}
VOUCHER_ROLES = {"dispatcher", "coordinator"}
VALVE_ROLES = {"field_operator", "dispatcher"}
FLUSH_ROLES = {"field_operator", "dispatcher"}
RESTORE_ROLES = {"coordinator", "dispatcher"}

OCCUPATION_ACTIVE = "active"
OCCUPATION_RELEASED = "released"

VOUCHER_ISSUED = "issued"
VOUCHER_CLOSED = "closed"
VOUCHER_FLUSHED = "flushed"
VOUCHER_RESTORED = "restored"

VALVE_CONFIRMED = "confirmed"
VALVE_FAILED = "failed"


def normalize_event(payload):
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise DomainError("field_required", "事件标题不能为空")
    occurred_at = payload.get("occurred_at")
    if not isinstance(occurred_at, str) or not occurred_at.strip():
        raise DomainError("field_required", "发生时间不能为空")
    stable_key = "network|%s|%s" % (title.strip(), occurred_at.strip())
    return {
        "title": title.strip(),
        "occurred_at": occurred_at.strip(),
        "kind": payload.get("kind", "emergency"),
        "note": payload.get("note", ""),
        "_stable_key": stable_key,
    }


def normalize_branch(payload):
    code = payload.get("code")
    if not isinstance(code, str) or not code.strip():
        raise DomainError("field_required", "支路编号不能为空")
    return {"code": code.strip(), "name": payload.get("name", "")}


def normalize_occupation(payload):
    return {"reason": payload.get("reason", "")}


def normalize_voucher(payload):
    voucher_no = payload.get("voucher_no")
    if not isinstance(voucher_no, str) or not voucher_no.strip():
        raise DomainError("field_required", "隔离凭证编号不能为空")
    raw_valves = payload.get("valves", [])
    if not isinstance(raw_valves, list) or not raw_valves:
        raise DomainError("valves_required", "隔离凭证至少要列出一个阀门")
    valves = []
    seen = set()
    for raw in raw_valves:
        if not isinstance(raw, dict):
            raise DomainError("invalid_valve", "阀门必须是对象")
        valve_id = raw.get("valve_id")
        if not isinstance(valve_id, str) or not valve_id.strip():
            raise DomainError("field_required", "阀门编号不能为空")
        valve_id = valve_id.strip()
        if valve_id in seen:
            raise DomainError("duplicate_valve", "同一凭证内阀门编号不能重复")
        seen.add(valve_id)
        valves.append({"valve_id": valve_id, "location": raw.get("location", "")})
    return {"voucher_no": voucher_no.strip(), "valves": valves}


def normalize_valve_results(payload):
    command_no = payload.get("command_no")
    if not isinstance(command_no, str) or not command_no.strip():
        raise DomainError("field_required", "命令号不能为空")
    raw_results = payload.get("results", [])
    if not isinstance(raw_results, list) or not raw_results:
        raise DomainError("results_required", "至少需要一条阀门回执")
    results = []
    seen = set()
    for raw in raw_results:
        if not isinstance(raw, dict):
            raise DomainError("invalid_result", "阀门回执必须是对象")
        valve_id = raw.get("valve_id")
        if not isinstance(valve_id, str) or not valve_id.strip():
            raise DomainError("field_required", "阀门编号不能为空")
        valve_id = valve_id.strip()
        if valve_id in seen:
            raise DomainError("duplicate_valve", "同一批次内阀门编号不能重复")
        seen.add(valve_id)
        status = raw.get("status")
        if status not in (VALVE_CONFIRMED, VALVE_FAILED):
            raise DomainError("invalid_valve_status", "阀门状态只能是 confirmed 或 failed")
        results.append({"valve_id": valve_id, "status": status})
    return command_no.strip(), results


def merge_receipts(existing, command_no, results, now_iso):
    """按命令号重试关阀：已确认的阀门保持不变，其余按结果更新回执。"""
    by_valve = {}
    for receipt in existing:
        by_valve[receipt["valve_id"]] = dict(receipt)
    for result in results:
        valve_id = result["valve_id"]
        prev = by_valve.get(valve_id)
        if prev is not None and prev["status"] == VALVE_CONFIRMED:
            # 已经确认的阀门保持不变，不重复执行关阀命令
            continue
        attempts = (prev.get("attempts", 0) if prev else 0) + 1
        entry = {
            "valve_id": valve_id,
            "command_no": command_no,
            "status": result["status"],
            "attempts": attempts,
        }
        if result["status"] == VALVE_CONFIRMED:
            entry["confirmed_at"] = now_iso
        else:
            entry["confirmed_at"] = None
        by_valve[valve_id] = entry
    return list(by_valve.values())


def missing_valves(voucher, receipts):
    """返回凭证中尚未确认关断的阀门编号。"""
    confirmed = {r["valve_id"] for r in receipts if r["status"] == VALVE_CONFIRMED}
    return [v["valve_id"] for v in voucher["valves"] if v["valve_id"] not in confirmed]


def ensure_flush_allowed(voucher, receipts):
    missing = missing_valves(voucher, receipts)
    if missing:
        raise DomainError(
            "valves_not_closed",
            "仍有阀门未确认关断，不能冲洗：%s" % "、".join(missing),
            409,
            missing_valves=missing,
        )


def ensure_restore_allowed(branch_id, event_id, active_occupations):
    """恢复时只要还有别的未结束事件占着该支路，就拦住并列出占用单号。"""
    others = [o for o in active_occupations if o["event_id"] != event_id]
    if others:
        numbers = [o["id"] for o in others]
        raise DomainError(
            "branch_occupied",
            "支路仍被其他未结束事件占用，不能开阀：占用单 %s" % "、".join("#%s" % n for n in numbers),
            409,
            occupation_ids=numbers,
            occupations=others,
        )
