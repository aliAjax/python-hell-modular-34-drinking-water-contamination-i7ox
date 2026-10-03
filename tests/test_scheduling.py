import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.domain import DomainError
from src.repository import Repository
from src.service import Service


class BranchSchedulingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def event(self, source="SRC-1", detected_at="2026-10-03T07:00:00+00:00"):
        return self.service.create_item(
            {
                "source_id": source,
                "contaminant": "bacteria",
                "detected_at": detected_at,
                "concentration": 30,
                "limit": 10,
                "zone_ids": ["Z-1"],
                "population": 1000,
            },
            "analyst-1",
            "analyst",
        )

    def ready_for_flush(self, event, branch_id, valves):
        occupancy = self.service.request_occupancy(
            {"event_id": event["id"], "branch_id": branch_id, "valve_ids": valves},
            "dispatcher-1",
            "dispatcher",
        )
        for valve in valves:
            self.service.record_valve_receipt(
                {
                    "branch_id": branch_id,
                    "valve_id": valve,
                    "command_no": next(item["command_no"] for item in occupancy["valves"] if item["valve_id"] == valve),
                    "closed": True,
                },
                "field-1",
                "field_operator",
            )
        event = self.service.act(event["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", event["version"])
        event = self.service.act(
            event["id"],
            "advise",
            {"notice_id": "N-%s" % event["id"], "kind": "boil", "message": "煮沸"},
            "dispatcher-1",
            "dispatcher",
            event["version"],
        )
        return event, occupancy

    def test_duplicate_application_returns_same_active_order(self):
        event = self.event()
        first = self.service.request_occupancy(
            {"event_id": event["id"], "branch_id": "B-1", "valve_ids": ["V-1", "V-2"]},
            "dispatcher-1",
            "dispatcher",
        )
        second = self.service.request_occupancy(
            {"event_id": event["id"], "branch_id": "B-1", "valve_ids": ["V-1", "V-2"]},
            "dispatcher-2",
            "dispatcher",
        )
        self.assertEqual(first["order_no"], second["order_no"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(self.service.branch_ledger("B-1")["occupancies"]), 1)

    def test_other_event_cannot_take_occupied_branch(self):
        event = self.event()
        other = self.event("SRC-2", "2026-10-03T08:00:00+00:00")
        order = self.service.request_occupancy(
            {"event_id": event["id"], "branch_id": "B-2", "valve_ids": ["V-1"]},
            "dispatcher-1",
            "dispatcher",
        )
        with self.assertRaises(DomainError) as context:
            self.service.request_occupancy(
                {"event_id": other["id"], "branch_id": "B-2", "valve_ids": ["V-2"]},
                "dispatcher-2",
                "dispatcher",
            )
        self.assertEqual(context.exception.code, "branch_occupied")
        self.assertEqual(context.exception.details["order_no"], order["order_no"])
        self.assertEqual(context.exception.details["event_id"], event["id"])

    def test_failed_valve_retry_keeps_confirmed_valves_and_gates_flush(self):
        event = self.event()
        occupancy = self.service.request_occupancy(
            {"event_id": event["id"], "branch_id": "B-3", "valve_ids": ["V-1", "V-2"]},
            "dispatcher-1",
            "dispatcher",
        )
        commands = {item["valve_id"]: item["command_no"] for item in occupancy["valves"]}
        occupancy = self.service.record_valve_receipt(
            {"branch_id": "B-3", "valve_id": "V-1", "command_no": commands["V-1"], "closed": False},
            "field-1",
            "field_operator",
        )
        self.assertEqual(next(v for v in occupancy["valves"] if v["valve_id"] == "V-1")["status"], "pending")
        occupancy = self.service.record_valve_receipt(
            {"branch_id": "B-3", "valve_id": "V-1", "command_no": commands["V-1"], "closed": True},
            "field-1",
            "field_operator",
        )
        occupancy = self.service.record_valve_receipt(
            {"branch_id": "B-3", "valve_id": "V-2", "command_no": commands["V-2"], "closed": True},
            "field-1",
            "field_operator",
        )
        with self.assertRaises(DomainError) as context:
            self.service.record_valve_receipt(
                {"branch_id": "B-3", "valve_id": "V-1", "command_no": commands["V-1"], "closed": False},
                "field-1",
                "field_operator",
            )
        self.assertEqual(context.exception.code, "valve_already_closed")
        current = self.service.get_occupancy(occupancy["order_no"])
        self.assertTrue(all(valve["status"] == "closed" for valve in current["valves"]))

        event = self.service.act(event["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", event["version"])
        event = self.service.act(
            event["id"],
            "advise",
            {"notice_id": "N-3", "kind": "boil", "message": "煮沸"},
            "dispatcher-1",
            "dispatcher",
            event["version"],
        )
        event = self.service.act(
            event["id"],
            "flush",
            {"zone_id": "Z-1", "branch_id": "B-3"},
            "field-1",
            "field_operator",
            event["version"],
        )
        self.assertEqual(event["status"], "flushing")

    def test_flush_waits_for_every_closed_valve(self):
        event = self.event()
        occupancy = self.service.request_occupancy(
            {"event_id": event["id"], "branch_id": "B-4", "valve_ids": ["V-1", "V-2"]},
            "dispatcher-1",
            "dispatcher",
        )
        self.service.record_valve_receipt(
            {
                "branch_id": "B-4",
                "valve_id": "V-1",
                "command_no": occupancy["valves"][0]["command_no"],
                "closed": True,
            },
            "field-1",
            "field_operator",
        )
        event = self.service.act(event["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", event["version"])
        event = self.service.act(
            event["id"],
            "advise",
            {"notice_id": "N-4", "kind": "boil", "message": "煮沸"},
            "dispatcher-1",
            "dispatcher",
            event["version"],
        )
        with self.assertRaises(DomainError) as context:
            self.service.act(
                event["id"],
                "flush",
                {"zone_id": "Z-1", "branch_id": "B-4"},
                "field-1",
                "field_operator",
                event["version"],
            )
        self.assertEqual(context.exception.code, "valves_not_closed")
        self.assertEqual(context.exception.details["pending_valve_ids"], ["V-2"])

    def test_restore_releases_branch_and_cancel_also_releases(self):
        event, occupancy = self.ready_for_flush(self.event("SRC-5"), "B-5", ["V-1"])
        event = self.service.act(
            event["id"],
            "flush",
            {"zone_id": "Z-1", "branch_id": "B-5"},
            "field-1",
            "field_operator",
            event["version"],
        )
        event = self.service.act(
            event["id"], "disinfect", {"zone_id": "Z-1", "completed": True}, "field-1", "field_operator", event["version"]
        )
        event = self.service.act(
            event["id"],
            "sample",
            {"sample_id": "SAMPLE-5", "zone_id": "Z-1", "concentration": 1},
            "lab-1",
            "lab",
            event["version"],
        )
        event = self.service.act(
            event["id"],
            "restore",
            {"all_zones_cleared": True, "branch_id": "B-5"},
            "coord-1",
            "coordinator",
            event["version"],
        )
        self.assertEqual(event["status"], "restored")
        stored = self.service.get_occupancy(occupancy["order_no"])
        self.assertEqual(stored["status"], "released")
        self.assertTrue(stored["released_at"])

        other = self.event("SRC-6", "2026-10-03T09:00:00+00:00")
        other_order = self.service.request_occupancy(
            {"event_id": other["id"], "branch_id": "B-6", "valve_ids": ["V-9"]},
            "dispatcher-1",
            "dispatcher",
        )
        self.service.act(other["id"], "cancel", {"reason": "误报"}, "coord-1", "coordinator", other["version"])
        self.assertEqual(self.service.get_occupancy(other_order["order_no"])["status"], "released")

    def test_upgraded_flush_requires_branch(self):
        event = self.event("SRC-4", "2026-10-03T09:30:00+00:00")
        event = self.service.act(event["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", event["version"])
        event = self.service.act(
            event["id"],
            "advise",
            {"notice_id": "N-UPGRADED", "kind": "boil", "message": "煮沸"},
            "dispatcher-1",
            "dispatcher",
            event["version"],
        )
        with self.assertRaises(DomainError) as context:
            self.service.act(
                event["id"],
                "flush",
                {"zone_id": "Z-1"},
                "field-1",
                "field_operator",
                event["version"],
            )
        self.assertEqual(context.exception.code, "branch_id_required")

    def test_concurrent_same_branch_application_has_one_winner(self):
        first_event = self.event("SRC-7", "2026-10-03T10:00:00+00:00")
        second_event = self.event("SRC-8", "2026-10-03T11:00:00+00:00")

        def apply(event):
            try:
                return True, self.service.request_occupancy(
                    {"event_id": event["id"], "branch_id": "B-7", "valve_ids": ["V-1"], "expected_version": 0},
                    "dispatcher-%s" % event["id"],
                    "dispatcher",
                )
            except DomainError as exc:
                return False, exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(apply, [first_event, second_event]))
        winners = [result for ok, result in results if ok]
        failures = [result for ok, result in results if not ok]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(failures), 1)
        self.assertIn(failures[0].code, {"branch_occupied", "version_conflict"})
        self.assertEqual(failures[0].details["current_version"], 1)


if __name__ == "__main__":
    unittest.main()
