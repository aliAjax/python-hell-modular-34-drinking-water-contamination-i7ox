import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    ledger_version INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS branches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    branch_id TEXT NOT NULL UNIQUE,
                    version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS occupancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_no TEXT NOT NULL UNIQUE,
                    branch_id TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    reason TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    released_at TEXT,
                    released_by TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_occupancy_branch
                    ON occupancies(branch_id) WHERE status='active';
                CREATE TABLE IF NOT EXISTS isolation_certificates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occupancy_id INTEGER NOT NULL UNIQUE,
                    certificate_no TEXT NOT NULL UNIQUE,
                    valve_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS valve_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occupancy_id INTEGER NOT NULL,
                    valve_id TEXT NOT NULL,
                    command_no TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(occupancy_id, valve_id),
                    UNIQUE(occupancy_id, command_no)
                );
                CREATE TABLE IF NOT EXISTS valve_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id INTEGER NOT NULL,
                    valve_id TEXT NOT NULL,
                    command_no TEXT NOT NULL,
                    closed INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_occupancies_item ON occupancies(item_id);
                CREATE INDEX IF NOT EXISTS idx_valve_commands_occupancy ON valve_commands(occupancy_id);
                CREATE INDEX IF NOT EXISTS idx_valve_receipts_command ON valve_receipts(command_id);
                """
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()
            }
            if "ledger_version" not in columns:
                conn.execute("ALTER TABLE items ADD COLUMN ledger_version INTEGER NOT NULL DEFAULT 0")
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,ledger_version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            conn.execute("COMMIT")
            return {"id": source_id, "item_id": item_id, "source_type": source_type, "external_id": external_id, "payload": payload, "observed_at": observed_at}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _occupancy_to_dict(self, conn, row):
        value = dict(row)
        receipts = {}
        receipt_rows = conn.execute(
            """
            SELECT vr.* FROM valve_receipts vr
            JOIN valve_commands vc ON vc.id=vr.command_id
            WHERE vc.occupancy_id=? ORDER BY vr.id
            """,
            (value["id"],),
        ).fetchall()
        for receipt in receipt_rows:
            receipt_value = dict(receipt)
            receipt_value["closed"] = bool(receipt_value["closed"])
            receipts.setdefault(receipt["command_id"], []).append(receipt_value)
        value["valves"] = []
        for command in conn.execute(
            """
            SELECT vc.id, vc.valve_id, vc.command_no, vc.status, vc.updated_at
            FROM valve_commands vc WHERE vc.occupancy_id=? ORDER BY vc.valve_id
            """,
            (value["id"],),
        ).fetchall():
            command_value = dict(command)
            command_value.pop("id", None)
            command_value["attempts"] = len(receipts.get(dict(command)["id"], []))
            command_value["receipts"] = receipts.get(dict(command)["id"], [])
            value["valves"].append(command_value)
        cert = conn.execute(
            "SELECT * FROM isolation_certificates WHERE occupancy_id=?", (value["id"],)
        ).fetchone()
        value["certificate"] = None if cert is None else dict(cert)
        return value

    def _get_active_occupancy(self, conn, branch_id):
        row = conn.execute(
            "SELECT * FROM occupancies WHERE branch_id=? AND status='active'", (branch_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def _get_or_create_branch(self, conn, branch_id):
        timestamp = now_iso()
        conn.execute(
            "INSERT OR IGNORE INTO branches(branch_id,version,created_at,updated_at) VALUES(?,?,?,?)",
            (branch_id, 0, timestamp, timestamp),
        )
        return conn.execute("SELECT * FROM branches WHERE branch_id=?", (branch_id,)).fetchone()

    def apply_occupancy(self, branch_id, item_id, valve_ids, reason, actor, role, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id, ledger_version FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("event_not_found", "事件不存在")
            branch = self._get_or_create_branch(conn, branch_id)
            if expected_version is not None and int(expected_version) != int(branch["version"]):
                raise ConflictError(
                    "version_conflict",
                    "支路台账已更新，请按最新版本重提",
                    {"branch_id": branch_id, "current_version": branch["version"]},
                )
            existing = self._get_active_occupancy(conn, branch_id)
            if existing is not None:
                if int(existing["item_id"]) == item_id:
                    result = self._occupancy_to_dict(conn, existing)
                    result["branch_version"] = branch["version"]
                    result["reused"] = True
                    conn.execute("COMMIT")
                    return result
                raise DomainError(
                    "branch_occupied",
                    "支路已被其他未结束事件占用",
                    409,
                    {
                        "order_no": existing["order_no"],
                        "event_id": existing["item_id"],
                        "current_version": branch["version"],
                    },
                )
            timestamp = now_iso()
            cursor = conn.execute(
                """
                INSERT INTO occupancies(order_no,branch_id,item_id,status,version,reason,created_by,created_role,created_at)
                VALUES('PENDING',?,?, 'active', 1, ?, ?, ?, ?)
                """,
                (branch_id, item_id, reason, actor, role, timestamp),
            )
            occupancy_id = cursor.lastrowid
            order_no = "OCC-%06d" % occupancy_id
            certificate_no = "CERT-%06d" % occupancy_id
            conn.execute(
                "UPDATE occupancies SET order_no=? WHERE id=?", (order_no, occupancy_id)
            )
            conn.execute(
                """
                INSERT INTO isolation_certificates(occupancy_id,certificate_no,valve_ids,created_at)
                VALUES(?,?,?,?)
                """,
                (occupancy_id, certificate_no, canonical_json(valve_ids), timestamp),
            )
            for valve_id in valve_ids:
                command_no = "CMD-%06d-%s" % (occupancy_id, valve_id)
                conn.execute(
                    """
                    INSERT INTO valve_commands(occupancy_id,valve_id,command_no,status,created_at,updated_at)
                    VALUES(?,?,?, 'pending', ?, ?)
                    """,
                    (occupancy_id, valve_id, command_no, timestamp, timestamp),
                )
            branch_version = int(branch["version"]) + 1
            conn.execute(
                "UPDATE branches SET version=?,updated_at=? WHERE id=?",
                (branch_version, timestamp, branch["id"]),
            )
            self.append_audit(
                conn,
                item_id,
                "branch_occupied",
                actor,
                role,
                {"order_no": order_no, "branch_id": branch_id, "valve_ids": valve_ids},
            )
            conn.execute("COMMIT")
            return self.get_occupancy(order_no)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def record_valve_receipt(self, branch_id, valve_id, command_no, closed, note, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            occupancy = self._get_active_occupancy(conn, branch_id)
            if occupancy is None:
                raise NotFoundError("active_occupancy_not_found", "该支路没有有效占用单")
            command = conn.execute(
                "SELECT * FROM valve_commands WHERE occupancy_id=? AND valve_id=? AND command_no=?",
                (occupancy["id"], valve_id, command_no),
            ).fetchone()
            if command is None:
                raise DomainError("command_not_found", "关阀命令不属于当前有效占用单", 404)
            if command["status"] == "closed" and not closed:
                raise DomainError("valve_already_closed", "已确认关断的阀门不能改为失败", 409)
            timestamp = now_iso()
            conn.execute(
                """
                INSERT INTO valve_receipts(command_id,valve_id,command_no,closed,note,actor,created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (command["id"], valve_id, command_no, 1 if closed else 0, note, actor, timestamp),
            )
            changed = False
            if closed and command["status"] != "closed":
                conn.execute(
                    "UPDATE valve_commands SET status='closed',updated_at=? WHERE id=?",
                    (timestamp, command["id"]),
                )
                changed = True
            elif not closed and command["status"] == "pending":
                conn.execute("UPDATE valve_commands SET updated_at=? WHERE id=?", (timestamp, command["id"]))
            branch = conn.execute("SELECT * FROM branches WHERE branch_id=?", (branch_id,)).fetchone()
            if changed:
                conn.execute(
                    "UPDATE branches SET version=?,updated_at=? WHERE id=?",
                    (int(branch["version"]) + 1, timestamp, branch["id"]),
                )
            self.append_audit(
                conn,
                occupancy["item_id"],
                "valve_receipt",
                actor,
                role,
                {
                    "order_no": occupancy["order_no"],
                    "branch_id": branch_id,
                    "valve_id": valve_id,
                    "command_no": command_no,
                    "closed": closed,
                },
            )
            conn.execute("COMMIT")
            return self.get_occupancy(occupancy["order_no"])
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _release_occupancies(self, conn, occupancy_ids, actor, role, timestamp):
        for occupancy_id in occupancy_ids:
            row = conn.execute(
                "SELECT * FROM occupancies WHERE id=? AND status='active'", (occupancy_id,)
            ).fetchone()
            if row is None:
                continue
            conn.execute(
                "UPDATE occupancies SET status='released',version=version+1,released_at=?,released_by=? WHERE id=?",
                (timestamp, actor, occupancy_id),
            )
            branch = conn.execute("SELECT * FROM branches WHERE branch_id=?", (row["branch_id"],)).fetchone()
            conn.execute(
                "UPDATE branches SET version=version+1,updated_at=? WHERE id=?",
                (timestamp, branch["id"]),
            )
            self.append_audit(
                conn,
                row["item_id"],
                "branch_released",
                actor,
                role,
                {"order_no": row["order_no"], "branch_id": row["branch_id"]},
            )

    def apply_flush_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            branch_id = event_payload.get("branch_id")
            if branch_id:
                occupancy = self._get_active_occupancy(conn, branch_id)
                if occupancy is None or int(occupancy["item_id"]) != item_id:
                    raise DomainError("isolation_not_active", "该支路没有本事件的有效隔离凭证", 409)
                pending = conn.execute(
                    "SELECT valve_id FROM valve_commands WHERE occupancy_id=? AND status!='closed'",
                    (occupancy["id"],),
                ).fetchall()
                if pending:
                    raise DomainError(
                        "valves_not_closed",
                        "所有阀门确认关断后才能冲洗",
                        409,
                        {"order_no": occupancy["order_no"], "pending_valve_ids": [r["valve_id"] for r in pending]},
                    )
            version = int(row["version"]) + 1
            timestamp = now_iso()
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), timestamp, item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), timestamp),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def apply_restore_or_cancel(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None, branch_id=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            release_ids = []
            if action == "restore":
                if branch_id:
                    occupancy = self._get_active_occupancy(conn, branch_id)
                    if occupancy is None:
                        raise DomainError("restore_blocked", "该支路没有有效占用单，不能执行恢复开阀", 409)
                    if int(occupancy["item_id"]) != item_id:
                        raise DomainError(
                            "restore_blocked",
                            "仍有其他未结束事件占用该支路，不能开阀",
                            409,
                            {"occupying_order_no": occupancy["order_no"], "event_id": occupancy["item_id"]},
                        )
                    release_ids.append(occupancy["id"])
                else:
                    rows = conn.execute(
                        "SELECT * FROM occupancies WHERE item_id=? AND status='active'", (item_id,)
                    ).fetchall()
                    blockers = []
                    for current in rows:
                        other = self._get_active_occupancy(conn, current["branch_id"])
                        if other is not None and int(other["item_id"]) != item_id:
                            blockers.append({"branch_id": current["branch_id"], "order_no": other["order_no"], "event_id": other["item_id"]})
                        else:
                            release_ids.append(current["id"])
                    if blockers:
                        raise DomainError(
                            "restore_blocked",
                            "仍有其他未结束事件占用支路，不能开阀",
                            409,
                            {"occupancies": blockers},
                        )
            else:
                rows = conn.execute(
                    "SELECT id FROM occupancies WHERE item_id=? AND status='active'", (item_id,)
                ).fetchall()
                release_ids = [current["id"] for current in rows]
            timestamp = now_iso()
            self._release_occupancies(conn, release_ids, actor, role, timestamp)
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), timestamp, item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), timestamp),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_occupancy(self, order_no):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM occupancies WHERE order_no=?", (order_no,)).fetchone()
            if row is None:
                raise NotFoundError("occupancy_not_found", "占用单不存在")
            result = self._occupancy_to_dict(conn, row)
            branch = conn.execute("SELECT version AS branch_version FROM branches WHERE branch_id=?", (row["branch_id"],)).fetchone()
            result["branch_version"] = branch["branch_version"]
            cert = result.pop("certificate")
            if cert is not None:
                result["certificate_no"] = cert["certificate_no"]
                result["certified_valve_ids"] = json.loads(cert["valve_ids"])
            return result
        finally:
            conn.close()

    def get_branch_ledger(self, branch_id):
        conn = self.connect()
        try:
            branch = conn.execute("SELECT * FROM branches WHERE branch_id=?", (branch_id,)).fetchone()
            if branch is None:
                raise NotFoundError("branch_not_found", "支路不存在")
            occupancies = [
                self._occupancy_to_dict(conn, row)
                for row in conn.execute(
                    "SELECT * FROM occupancies WHERE branch_id=? ORDER BY id DESC", (branch_id,)
                ).fetchall()
            ]
            for occupancy in occupancies:
                cert = occupancy.pop("certificate")
                if cert is not None:
                    occupancy["certificate_no"] = cert["certificate_no"]
                    occupancy["certified_valve_ids"] = json.loads(cert["valve_ids"])
            return {"branch_id": branch_id, "version": branch["version"], "occupancies": occupancies}
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()
