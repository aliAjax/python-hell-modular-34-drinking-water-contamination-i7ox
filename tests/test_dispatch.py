import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class DispatchLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.event = self.service.create_network_event(
            {"title": "E-放空", "occurred_at": "2026-10-03T08:00:00+00:00"}, "d1", "dispatcher"
        )
        self.branch = self.service.create_branch({"code": "BR-1", "name": "支路一"}, "d1", "dispatcher")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _occupy(self, event_id=None, version=None):
        event_id = event_id or self.event["id"]
        version = version if version is not None else self.service.get_branch("BR-1")["version"]
        return self.service.apply_occupation("BR-1", event_id, {"reason": "隔离"}, "d1", "dispatcher", version)

    def _issue_voucher(self, no="V-1", valves=None, event_id=None):
        valves = valves or [{"valve_id": "V101"}, {"valve_id": "V102"}, {"valve_id": "V103"}]
        return self.service.issue_voucher(
            "BR-1", event_id or self.event["id"], {"voucher_no": no, "valves": valves}, "d1", "dispatcher"
        )

    def test_duplicate_occupation_returns_original(self):
        first, created = self._occupy()
        self.assertTrue(created)
        before = self.service.get_branch("BR-1")["version"]
        second, created_again = self._occupy()
        self.assertFalse(created_again)
        self.assertEqual(first["id"], second["id"])
        # 重复申请不增加支路版本
        self.assertEqual(before, self.service.get_branch("BR-1")["version"])
        ledger = self.service.branch_ledger("BR-1")
        self.assertEqual(len(ledger["occupations"]), 1)

    def test_voucher_requires_occupation(self):
        other = self.service.create_network_event(
            {"title": "E-无占用", "occurred_at": "2026-10-03T09:00:00+00:00"}, "d1", "dispatcher"
        )
        with self.assertRaises(DomainError) as ctx:
            self._issue_voucher(no="V-X", event_id=other["id"])
        self.assertEqual(ctx.exception.code, "occupation_required")

    def test_valve_retry_keeps_confirmed_valves(self):
        self._occupy()
        voucher = self._issue_voucher()
        self.service.close_valves(
            "V-1",
            {"command_no": "CMD-1", "results": [{"valve_id": "V101", "status": "confirmed"}, {"valve_id": "V102", "status": "failed"}]},
            "f1", "field_operator",
        )
        # 重试：V101 已确认，即使回报 failed 也保持不变；V102 重试确认；V103 首次确认
        self.service.close_valves(
            "V-1",
            {"command_no": "CMD-1", "results": [{"valve_id": "V101", "status": "failed"}, {"valve_id": "V102", "status": "confirmed"}, {"valve_id": "V103", "status": "confirmed"}]},
            "f1", "field_operator",
        )
        voucher = self.service.get_voucher("V-1")
        self.assertEqual(voucher["status"], "closed")
        by_valve = {r["valve_id"]: r for r in voucher["receipts"]}
        self.assertEqual(by_valve["V101"]["status"], "confirmed")
        self.assertEqual(by_valve["V101"]["attempts"], 1)
        self.assertEqual(by_valve["V102"]["status"], "confirmed")
        self.assertEqual(by_valve["V102"]["attempts"], 2)
        self.assertEqual(by_valve["V103"]["status"], "confirmed")
        self.assertEqual(by_valve["V103"]["attempts"], 1)
        # 三次回执都沿用同一命令号
        self.assertTrue(all(r["command_no"] == "CMD-1" for r in voucher["receipts"]))

    def test_flush_requires_all_valves_closed(self):
        self._occupy()
        self._issue_voucher()
        self.service.close_valves(
            "V-1",
            {"command_no": "CMD-1", "results": [{"valve_id": "V101", "status": "confirmed"}]},
            "f1", "field_operator",
        )
        with self.assertRaises(DomainError) as ctx:
            self.service.flush_branch("V-1", "f1", "field_operator")
        self.assertEqual(ctx.exception.code, "valves_not_closed")
        self.assertIn("V102", ctx.exception.extra["missing_valves"])
        # 全部确认后才能冲洗
        self.service.close_valves(
            "V-1",
            {"command_no": "CMD-1", "results": [{"valve_id": "V102", "status": "confirmed"}, {"valve_id": "V103", "status": "confirmed"}]},
            "f1", "field_operator",
        )
        flushed = self.service.flush_branch("V-1", "f1", "field_operator")
        self.assertEqual(flushed["status"], "flushed")

    def test_restore_blocked_by_other_occupations(self):
        self._occupy()
        voucher = self._issue_voucher()
        self.service.close_valves(
            "V-1",
            {"command_no": "CMD-1", "results": [{"valve_id": v, "status": "confirmed"} for v in ("V101", "V102", "V103")]},
            "f1", "field_operator",
        )
        self.service.flush_branch("V-1", "f1", "field_operator")
        # 另一个事件也占着这条支路
        other = self.service.create_network_event(
            {"title": "E-抢修", "occurred_at": "2026-10-03T10:00:00+00:00"}, "d1", "dispatcher"
        )
        other_occ, _ = self._occupy(event_id=other["id"])
        with self.assertRaises(DomainError) as ctx:
            self.service.restore_branch("V-1", "c1", "coordinator")
        self.assertEqual(ctx.exception.code, "branch_occupied")
        self.assertIn(other_occ["id"], ctx.exception.extra["occupation_ids"])
        # 对方释放后，本事件恢复成功
        self.service.release_occupation(other_occ["id"], "d1", "dispatcher")
        restored = self.service.restore_branch("V-1", "c1", "coordinator")
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(self.service.get_item(self.event["id"])["status"], "closed")

    def test_concurrent_occupation_version_conflict(self):
        # 两个调度员都先读到同一个旧版本，再同时提交申请
        version = self.service.get_branch("BR-1")["version"]
        start = threading.Barrier(2)
        results = {}

        def worker(name):
            start.wait()
            try:
                occ, created = self.service.apply_occupation(
                    "BR-1", self.event["id"], {"reason": "隔离"}, "d1", "dispatcher", version
                )
                results[name] = ("ok", occ["id"], created)
            except ConflictError as exc:
                results[name] = ("conflict", exc.code)

        t1 = threading.Thread(target=worker, args=("a",))
        t2 = threading.Thread(target=worker, args=("b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        outcomes = list(results.values())
        self.assertIn(("ok", 1, True), outcomes)
        self.assertIn(("conflict", "version_conflict"), outcomes)
        # 失利方按最新版本重提，拿回同一张原单
        latest = self.service.get_branch("BR-1")["version"]
        occ, created = self._occupy(version=latest)
        self.assertFalse(created)
        self.assertEqual(occ["id"], 1)

    def test_ledger_assembles_event_voucher_receipts(self):
        self._occupy()
        self._issue_voucher()
        self.service.close_valves(
            "V-1",
            {"command_no": "CMD-1", "results": [{"valve_id": "V101", "status": "confirmed"}]},
            "f1", "field_operator",
        )
        ledger = self.service.event_ledger(self.event["id"])
        self.assertEqual(ledger["event"]["id"], self.event["id"])
        self.assertEqual(len(ledger["occupations"]), 1)
        self.assertEqual(ledger["vouchers"][0]["voucher_no"], "V-1")
        self.assertEqual(ledger["vouchers"][0]["receipts"][0]["valve_id"], "V101")

    def test_legacy_data_without_voucher_still_works(self):
        # 旧的污染响应事件没有隔离凭证，原流程照旧
        legacy = self.service.create_item(
            {
                "source_id": "SRC-OLD",
                "contaminant": "nitrate",
                "detected_at": "2026-09-27T06:00:00+00:00",
                "concentration": 20,
                "limit": 10,
                "zone_ids": ["Z-1"],
                "population": 5000,
            },
            "a", "analyst",
        )
        legacy = self.service.act(legacy["id"], "verify", {"sample_count": 1}, "a", "analyst", legacy["version"])
        self.assertEqual(legacy["status"], "verified")
        # 旧记录可查，且不带调度账字段
        fetched = self.service.get_item(legacy["id"])
        self.assertEqual(fetched["entity_type"], "water_contamination")
        self.assertIn("assessment", fetched)
        self.assertNotIn("occupations", fetched)
        # 新流程不影响旧记录查询
        self._occupy()
        self.assertIn(legacy["id"], [item["id"] for item in self.service.list_items()])


if __name__ == "__main__":
    unittest.main()
