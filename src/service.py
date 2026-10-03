from . import domain, rules, scheduling
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action == "flush" and item["ledger_version"] > 0:
            branch_id = payload.get("branch_id")
            if not isinstance(branch_id, str) or not branch_id.strip():
                raise DomainError("branch_id_required", "升级后的事件冲洗必须指定支路隔离凭证")
        if action == "restore" and payload.get("branch_id"):
            if not isinstance(payload["branch_id"], str) or not payload["branch_id"].strip():
                raise DomainError("invalid_branch", "branch_id 不能为空")
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        if action == "flush" and event_payload.get("branch_id"):
            self.repository.apply_flush_action(
                item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
            )
        elif action in {"restore", "cancel"} and (
            item["ledger_version"] > 0 or payload.get("branch_id") or action == "cancel"
        ):
            self.repository.apply_restore_or_cancel(
                item_id,
                action,
                actor,
                role,
                new_status,
                new_payload,
                event_payload,
                expected_version,
                payload.get("branch_id", "").strip() if payload.get("branch_id") else None,
            )
        else:
            self.repository.apply_action(
                item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
            )
        return self.get_item(item_id)

    def request_occupancy(self, payload, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in scheduling.OCCUPANCY_ROLES:
            raise DomainError("forbidden", "当前角色不能申请支路占用", 403)
        normalized = scheduling.normalize_occupancy(payload)
        expected_version = payload.get("expected_version")
        if expected_version is not None:
            if isinstance(expected_version, bool):
                raise DomainError("invalid_version", "expected_version 必须是整数", 400)
            try:
                expected_version = int(expected_version)
            except (TypeError, ValueError):
                raise DomainError("invalid_version", "expected_version 必须是整数", 400)
        return self.repository.apply_occupancy(
            normalized["branch_id"],
            normalized["event_id"],
            normalized["valve_ids"],
            normalized["reason"],
            actor,
            role,
            expected_version,
        )

    def record_valve_receipt(self, payload, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in scheduling.RECEIPT_ROLES:
            raise DomainError("forbidden", "当前角色不能提交关阀回执", 403)
        normalized = scheduling.normalize_valve_receipt(payload)
        return self.repository.record_valve_receipt(
            normalized["branch_id"],
            normalized["valve_id"],
            normalized["command_no"],
            normalized["closed"],
            normalized["note"],
            actor,
            role,
        )

    def get_occupancy(self, order_no):
        return self.repository.get_occupancy(order_no)

    def branch_ledger(self, branch_id):
        return self.repository.get_branch_ledger(branch_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
