from . import domain, rules, dispatch
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def _require_role(self, actor, role, allowed, message="当前角色不能执行该操作"):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in allowed:
            raise DomainError("forbidden", message, 403)

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
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        if item.get("entity_type") == dispatch.ENTITY_TYPE:
            ledger = self.repository.event_ledger(item_id)
            item["occupations"] = ledger["occupations"]
            item["vouchers"] = ledger["vouchers"]
            return item
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()

    # ---- 管网调度账 ----

    def create_network_event(self, payload, actor, role):
        self._require_role(actor, role, dispatch.EVENT_CREATE_ROLES, "当前角色不能创建管网事件")
        normalized = dispatch.normalize_event(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            dispatch.ENTITY_TYPE, stable_key, dispatch.INITIAL_STATUS, normalized, actor, role
        )

    def create_branch(self, payload, actor, role):
        self._require_role(actor, role, dispatch.BRANCH_ROLES, "当前角色不能登记支路")
        normalized = dispatch.normalize_branch(payload)
        return self.repository.create_branch(normalized["code"], normalized["name"], actor)

    def list_branches(self):
        return self.repository.list_branches()

    def get_branch(self, branch_code):
        branch = self.repository.get_branch_by_code(branch_code)
        active = self.repository.list_active_occupations(branch["id"])
        branch["active_occupations"] = active
        return branch

    def apply_occupation(self, branch_code, event_id, payload, actor, role, expected_version=None):
        self._require_role(actor, role, dispatch.OCCUPATION_ROLES, "当前角色不能申请占用支路")
        if expected_version is None:
            raise DomainError("expected_version_required", "申请占用需要 expected_version", 400)
        normalized = dispatch.normalize_occupation(payload)
        branch = self.repository.get_branch_by_code(branch_code)
        event = self.repository.get_item(event_id)
        if event.get("entity_type") != dispatch.ENTITY_TYPE:
            raise DomainError("not_a_network_event", "该记录不是管网事件", 400)
        if event["status"] != dispatch.INITIAL_STATUS:
            raise DomainError("event_closed", "事件已结束，不能申请占用", 409)
        return self.repository.apply_occupation(
            branch["id"], event_id, normalized["reason"], actor, role, expected_version
        )

    def release_occupation(self, occupation_id, actor, role, expected_version=None):
        self._require_role(actor, role, dispatch.OCCUPATION_ROLES, "当前角色不能释放占用单")
        return self.repository.release_occupation(occupation_id, actor, role, expected_version)

    def get_voucher(self, voucher_no):
        return self.repository.get_voucher_by_no(voucher_no)

    def issue_voucher(self, branch_code, event_id, payload, actor, role):
        self._require_role(actor, role, dispatch.VOUCHER_ROLES, "当前角色不能开具隔离凭证")
        normalized = dispatch.normalize_voucher(payload)
        branch = self.repository.get_branch_by_code(branch_code)
        event = self.repository.get_item(event_id)
        if event.get("entity_type") != dispatch.ENTITY_TYPE:
            raise DomainError("not_a_network_event", "该记录不是管网事件", 400)
        if event["status"] != dispatch.INITIAL_STATUS:
            raise DomainError("event_closed", "事件已结束，不能开具隔离凭证", 409)
        occupation = self.repository.find_active_occupation(branch["id"], event_id)
        if occupation is None:
            raise DomainError("occupation_required", "需要先取得该支路的有效占用单", 409)
        return self.repository.create_voucher(
            normalized["voucher_no"],
            branch["id"],
            event_id,
            occupation["id"],
            normalized["valves"],
            actor,
            role,
        )

    def close_valves(self, voucher_no, payload, actor, role):
        self._require_role(actor, role, dispatch.VALVE_ROLES, "当前角色不能关阀")
        command_no, results = dispatch.normalize_valve_results(payload)
        voucher = self.repository.get_voucher_by_no(voucher_no)
        return self.repository.close_valves(voucher["id"], command_no, results, actor, role)

    def flush_branch(self, voucher_no, actor, role):
        self._require_role(actor, role, dispatch.FLUSH_ROLES, "当前角色不能执行冲洗")
        voucher = self.repository.get_voucher_by_no(voucher_no)
        return self.repository.flush_voucher(voucher["id"], actor, role)

    def restore_branch(self, voucher_no, actor, role):
        self._require_role(actor, role, dispatch.RESTORE_ROLES, "当前角色不能执行恢复")
        voucher = self.repository.get_voucher_by_no(voucher_no)
        return self.repository.restore_voucher(voucher["id"], actor, role)

    def branch_ledger(self, branch_code):
        branch = self.repository.get_branch_by_code(branch_code)
        return self.repository.branch_ledger(branch["id"])

    def event_ledger(self, event_id):
        return self.repository.event_ledger(event_id)
