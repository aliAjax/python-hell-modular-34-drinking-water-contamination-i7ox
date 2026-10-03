import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from . import dispatch
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
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS occupations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    branch_id INTEGER NOT NULL,
                    event_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    reason TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    released_at TEXT,
                    FOREIGN KEY(branch_id) REFERENCES branches(id),
                    FOREIGN KEY(event_id) REFERENCES items(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_occupations_active_pair
                    ON occupations(branch_id, event_id) WHERE status='active';
                CREATE TABLE IF NOT EXISTS isolation_vouchers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    voucher_no TEXT NOT NULL UNIQUE,
                    branch_id INTEGER NOT NULL,
                    event_id INTEGER NOT NULL,
                    occupation_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    valves TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(branch_id) REFERENCES branches(id),
                    FOREIGN KEY(event_id) REFERENCES items(id),
                    FOREIGN KEY(occupation_id) REFERENCES occupations(id)
                );
                CREATE TABLE IF NOT EXISTS valve_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    voucher_id INTEGER NOT NULL,
                    valve_id TEXT NOT NULL,
                    command_no TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    confirmed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(voucher_id, valve_id),
                    FOREIGN KEY(voucher_id) REFERENCES isolation_vouchers(id)
                );
                """
            )
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
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
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

    # ---- 管网调度账：支路 / 占用单 / 隔离凭证 / 关阀回执 ----

    def _row_to_branch(self, row):
        if row is None:
            return None
        return dict(row)

    def _row_to_occupation(self, row):
        if row is None:
            return None
        return dict(row)

    def _row_to_voucher(self, row):
        if row is None:
            return None
        result = dict(row)
        result["valves"] = json.loads(result["valves"])
        return result

    def _row_to_receipt(self, row):
        if row is None:
            return None
        return dict(row)

    def create_branch(self, code, name, actor):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO branches(code,name,version,created_at) VALUES(?,?,1,?)",
                    (code, name, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_branch", "支路编号已存在")
            branch_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute("COMMIT")
            return self.get_branch(branch_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_branch(self, branch_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM branches WHERE id=?", (branch_id,)).fetchone()
            if row is None:
                raise NotFoundError("branch_not_found", "支路不存在")
            return self._row_to_branch(row)
        finally:
            conn.close()

    def get_branch_by_code(self, code):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM branches WHERE code=?", (code,)).fetchone()
            if row is None:
                raise NotFoundError("branch_not_found", "支路不存在")
            return self._row_to_branch(row)
        finally:
            conn.close()

    def list_branches(self):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM branches ORDER BY id").fetchall()
            return [self._row_to_branch(row) for row in rows]
        finally:
            conn.close()

    def apply_occupation(self, branch_id, event_id, reason, actor, role, expected_version):
        """申请占用支路。同一事件已有有效占用单时拿回原单；否则按版本乐观锁新建。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            branch = conn.execute("SELECT * FROM branches WHERE id=?", (branch_id,)).fetchone()
            if branch is None:
                raise NotFoundError("branch_not_found", "支路不存在")
            if expected_version is not None and int(expected_version) != int(branch["version"]):
                raise ConflictError(
                    "version_conflict",
                    "支路占用状态已被其他调度员更新，请重新读取后按最新版本重提",
                )
            existing = conn.execute(
                "SELECT * FROM occupations WHERE branch_id=? AND event_id=? AND status='active' ORDER BY id DESC LIMIT 1",
                (branch_id, event_id),
            ).fetchone()
            if existing is not None:
                conn.execute("COMMIT")
                return self._row_to_occupation(existing), False
            version = int(branch["version"]) + 1
            conn.execute("UPDATE branches SET version=? WHERE id=?", (version, branch_id))
            conn.execute(
                "INSERT INTO occupations(branch_id,event_id,status,version,reason,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (branch_id, event_id, dispatch.OCCUPATION_ACTIVE, version, reason, actor, now_iso(), now_iso()),
            )
            occupation_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                event_id,
                "occupation_applied",
                actor,
                role,
                {"branch_id": branch_id, "occupation_id": occupation_id, "reason": reason},
            )
            conn.execute("COMMIT")
            return self.get_occupation(occupation_id), True
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_occupation(self, occupation_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM occupations WHERE id=?", (occupation_id,)).fetchone()
            if row is None:
                raise NotFoundError("occupation_not_found", "占用单不存在")
            return self._row_to_occupation(row)
        finally:
            conn.close()

    def find_active_occupation(self, branch_id, event_id):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM occupations WHERE branch_id=? AND event_id=? AND status='active' ORDER BY id DESC LIMIT 1",
                (branch_id, event_id),
            ).fetchone()
            return self._row_to_occupation(row)
        finally:
            conn.close()

    def list_active_occupations(self, branch_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM occupations WHERE branch_id=? AND status='active' ORDER BY id",
                (branch_id,),
            ).fetchall()
            return [self._row_to_occupation(row) for row in rows]
        finally:
            conn.close()

    def list_occupations_for_event(self, event_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM occupations WHERE event_id=? ORDER BY id DESC",
                (event_id,),
            ).fetchall()
            return [self._row_to_occupation(row) for row in rows]
        finally:
            conn.close()

    def release_occupation(self, occupation_id, actor, role, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM occupations WHERE id=?", (occupation_id,)).fetchone()
            if row is None:
                raise NotFoundError("occupation_not_found", "占用单不存在")
            if row["status"] != dispatch.OCCUPATION_ACTIVE:
                raise DomainError("occupation_not_active", "占用单已结束", 409)
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "占用单已被其他操作更新，请重新读取")
            conn.execute(
                "UPDATE occupations SET status=?,version=version+1,updated_at=?,released_at=? WHERE id=?",
                (dispatch.OCCUPATION_RELEASED, now_iso(), now_iso(), occupation_id),
            )
            self.append_audit(
                conn, row["event_id"], "occupation_released", actor, role, {"occupation_id": occupation_id}
            )
            conn.execute("COMMIT")
            return self.get_occupation(occupation_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def create_voucher(self, voucher_no, branch_id, event_id, occupation_id, valves, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            occ = conn.execute(
                "SELECT id FROM occupations WHERE id=? AND branch_id=? AND event_id=? AND status='active'",
                (occupation_id, branch_id, event_id),
            ).fetchone()
            if occ is None:
                raise DomainError("occupation_required", "需要先取得该支路的有效占用单", 409)
            try:
                conn.execute(
                    "INSERT INTO isolation_vouchers(voucher_no,branch_id,event_id,occupation_id,status,version,valves,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        voucher_no,
                        branch_id,
                        event_id,
                        occupation_id,
                        dispatch.VOUCHER_ISSUED,
                        1,
                        canonical_json(valves),
                        actor,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_voucher", "隔离凭证编号已存在")
            voucher_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                event_id,
                "voucher_issued",
                actor,
                role,
                {"voucher_id": voucher_id, "voucher_no": voucher_no, "branch_id": branch_id},
            )
            conn.execute("COMMIT")
            return self.get_voucher(voucher_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_voucher(self, voucher_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM isolation_vouchers WHERE id=?", (voucher_id,)).fetchone()
            if row is None:
                raise NotFoundError("voucher_not_found", "隔离凭证不存在")
            voucher = self._row_to_voucher(row)
            voucher["receipts"] = [
                self._row_to_receipt(r)
                for r in conn.execute("SELECT * FROM valve_receipts WHERE voucher_id=? ORDER BY id", (voucher_id,)).fetchall()
            ]
            return voucher
        finally:
            conn.close()

    def get_voucher_by_no(self, voucher_no):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM isolation_vouchers WHERE voucher_no=?", (voucher_no,)).fetchone()
            if row is None:
                raise NotFoundError("voucher_not_found", "隔离凭证不存在")
            return self.get_voucher(row["id"])
        finally:
            conn.close()

    def list_vouchers_for_branch(self, branch_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM isolation_vouchers WHERE branch_id=? ORDER BY id DESC",
                (branch_id,),
            ).fetchall()
            return [self._row_to_voucher(row) for row in rows]
        finally:
            conn.close()

    def list_vouchers_for_event(self, event_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM isolation_vouchers WHERE event_id=? ORDER BY id DESC",
                (event_id,),
            ).fetchall()
            return [self._row_to_voucher(row) for row in rows]
        finally:
            conn.close()

    def list_receipts(self, voucher_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM valve_receipts WHERE voucher_id=? ORDER BY id",
                (voucher_id,),
            ).fetchall()
            return [self._row_to_receipt(row) for row in rows]
        finally:
            conn.close()

    def close_valves(self, voucher_id, command_no, results, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            voucher = conn.execute("SELECT * FROM isolation_vouchers WHERE id=?", (voucher_id,)).fetchone()
            if voucher is None:
                raise NotFoundError("voucher_not_found", "隔离凭证不存在")
            if voucher["status"] == dispatch.VOUCHER_RESTORED:
                raise DomainError("voucher_restored", "凭证已恢复供水，不能再关阀", 409)
            existing_rows = conn.execute(
                "SELECT * FROM valve_receipts WHERE voucher_id=?", (voucher_id,)
            ).fetchall()
            existing = [self._row_to_receipt(row) for row in existing_rows]
            merged = dispatch.merge_receipts(existing, command_no, results, now_iso())
            for receipt in merged:
                conn.execute(
                    """INSERT INTO valve_receipts(voucher_id,valve_id,command_no,status,attempts,confirmed_at,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(voucher_id,valve_id) DO UPDATE SET
                         command_no=excluded.command_no,
                         status=excluded.status,
                         attempts=excluded.attempts,
                         confirmed_at=excluded.confirmed_at,
                         updated_at=excluded.updated_at""",
                    (
                        voucher_id,
                        receipt["valve_id"],
                        receipt["command_no"],
                        receipt["status"],
                        receipt["attempts"],
                        receipt.get("confirmed_at"),
                        now_iso(),
                        now_iso(),
                    ),
                )
            valve_ids = [v["valve_id"] for v in json.loads(voucher["valves"])]
            confirmed = {r["valve_id"] for r in merged if r["status"] == dispatch.VALVE_CONFIRMED}
            new_status = (
                dispatch.VOUCHER_CLOSED
                if all(vid in confirmed for vid in valve_ids)
                else dispatch.VOUCHER_ISSUED
            )
            if new_status != voucher["status"]:
                conn.execute(
                    "UPDATE isolation_vouchers SET status=?,updated_at=? WHERE id=?",
                    (new_status, now_iso(), voucher_id),
                )
            failed = sorted({r["valve_id"] for r in merged if r["status"] == dispatch.VALVE_FAILED})
            self.append_audit(
                conn,
                voucher["event_id"],
                "valves_closed",
                actor,
                role,
                {
                    "voucher_id": voucher_id,
                    "command_no": command_no,
                    "confirmed": sorted(confirmed),
                    "failed": failed,
                },
            )
            conn.execute("COMMIT")
            return self.get_voucher(voucher_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def flush_voucher(self, voucher_id, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            voucher = conn.execute("SELECT * FROM isolation_vouchers WHERE id=?", (voucher_id,)).fetchone()
            if voucher is None:
                raise NotFoundError("voucher_not_found", "隔离凭证不存在")
            if voucher["status"] == dispatch.VOUCHER_RESTORED:
                raise DomainError("voucher_restored", "凭证已恢复供水，不能再冲洗", 409)
            receipts = [self._row_to_receipt(row) for row in conn.execute(
                "SELECT * FROM valve_receipts WHERE voucher_id=?", (voucher_id,)
            ).fetchall()]
            dispatch.ensure_flush_allowed({"valves": json.loads(voucher["valves"])}, receipts)
            conn.execute(
                "UPDATE isolation_vouchers SET status=?,updated_at=? WHERE id=?",
                (dispatch.VOUCHER_FLUSHED, now_iso(), voucher_id),
            )
            self.append_audit(
                conn,
                voucher["event_id"],
                "branch_flushed",
                actor,
                role,
                {"voucher_id": voucher_id},
            )
            conn.execute("COMMIT")
            return self.get_voucher(voucher_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def restore_voucher(self, voucher_id, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            voucher = conn.execute("SELECT * FROM isolation_vouchers WHERE id=?", (voucher_id,)).fetchone()
            if voucher is None:
                raise NotFoundError("voucher_not_found", "隔离凭证不存在")
            if voucher["status"] == dispatch.VOUCHER_RESTORED:
                raise DomainError("voucher_already_restored", "凭证已恢复供水", 409)
            branch_id = voucher["branch_id"]
            event_id = voucher["event_id"]
            active = [self._row_to_occupation(row) for row in conn.execute(
                "SELECT * FROM occupations WHERE branch_id=? AND status='active' ORDER BY id",
                (branch_id,),
            ).fetchall()]
            dispatch.ensure_restore_allowed(branch_id, event_id, active)
            conn.execute(
                "UPDATE isolation_vouchers SET status=?,updated_at=? WHERE id=?",
                (dispatch.VOUCHER_RESTORED, now_iso(), voucher_id),
            )
            conn.execute(
                "UPDATE occupations SET status=?,updated_at=?,released_at=? WHERE branch_id=? AND event_id=? AND status='active'",
                (dispatch.OCCUPATION_RELEASED, now_iso(), now_iso(), branch_id, event_id),
            )
            conn.execute(
                "UPDATE items SET status='closed',version=version+1,updated_at=? WHERE id=?",
                (now_iso(), event_id),
            )
            self.append_audit(
                conn,
                event_id,
                "branch_restored",
                actor,
                role,
                {"voucher_id": voucher_id, "branch_id": branch_id},
            )
            conn.execute("COMMIT")
            return self.get_voucher(voucher_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def branch_ledger(self, branch_id):
        conn = self.connect()
        try:
            branch = self._row_to_branch(
                conn.execute("SELECT * FROM branches WHERE id=?", (branch_id,)).fetchone()
            )
            if branch is None:
                raise NotFoundError("branch_not_found", "支路不存在")
            occupations = [self._row_to_occupation(row) for row in conn.execute(
                "SELECT * FROM occupations WHERE branch_id=? ORDER BY id", (branch_id,)
            ).fetchall()]
            vouchers = [self._row_to_voucher(row) for row in conn.execute(
                "SELECT * FROM isolation_vouchers WHERE branch_id=? ORDER BY id DESC", (branch_id,)
            ).fetchall()]
            for voucher in vouchers:
                voucher["receipts"] = [self._row_to_receipt(row) for row in conn.execute(
                    "SELECT * FROM valve_receipts WHERE voucher_id=? ORDER BY id", (voucher["id"],)
                ).fetchall()]
            return {"branch": branch, "occupations": occupations, "vouchers": vouchers}
        finally:
            conn.close()

    def event_ledger(self, event_id):
        conn = self.connect()
        try:
            item = conn.execute("SELECT * FROM items WHERE id=?", (event_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            occupations = [self._row_to_occupation(row) for row in conn.execute(
                "SELECT * FROM occupations WHERE event_id=? ORDER BY id", (event_id,)
            ).fetchall()]
            vouchers = [self._row_to_voucher(row) for row in conn.execute(
                "SELECT * FROM isolation_vouchers WHERE event_id=? ORDER BY id DESC", (event_id,)
            ).fetchall()]
            for voucher in vouchers:
                voucher["receipts"] = [self._row_to_receipt(row) for row in conn.execute(
                    "SELECT * FROM valve_receipts WHERE voucher_id=? ORDER BY id", (voucher["id"],)
                ).fetchall()]
            return {"event": self._row_to_item(item), "occupations": occupations, "vouchers": vouchers}
        finally:
            conn.close()
