import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service


class LegacyDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.path = self.tmp.name
        repo = Repository(self.path)
        repo.initialize()
        conn = sqlite3.connect(self.path)
        payload = {
            "source_id": "LEGACY-1",
            "contaminant": "bacteria",
            "detected_at": "2026-10-01T07:00:00+00:00",
            "concentration": 30,
            "limit": 10,
            "zone_ids": ["Z-LEGACY"],
            "population": 100,
            "complaints": 0,
            "notifications": [],
            "response_actions": [],
            "sample_results": [],
        }
        conn.execute(
            """
            INSERT INTO items(entity_type,stable_key,status,version,ledger_version,payload,
                              created_by,created_role,created_at,updated_at)
            VALUES('water_contamination','water_contamination|LEGACY-1|bacteria|2026-10-01T07:00:00+00:00',
                   'detected',1,0,?,'analyst-legacy','analyst','2026-10-01T07:00:00+00:00',
                   '2026-10-01T07:00:00+00:00')
            """,
            (json.dumps(payload, ensure_ascii=False),),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.path)

    def test_old_record_without_isolation_certificate_keeps_original_flow(self):
        service = Service(Repository(self.path))
        item = service.list_items()[0]
        self.assertEqual(item["ledger_version"], 0)
        item = service.act(item["id"], "verify", {"sample_count": 1}, "analyst-legacy", "analyst", item["version"])
        item = service.act(
            item["id"],
            "advise",
            {"notice_id": "LEGACY-N", "kind": "boil", "message": "煮沸"},
            "dispatcher-1",
            "dispatcher",
            item["version"],
        )
        item = service.act(
            item["id"],
            "flush",
            {"zone_id": "Z-LEGACY"},
            "field-1",
            "field_operator",
            item["version"],
        )
        self.assertEqual(item["status"], "flushing")
        item = service.act(
            item["id"],
            "disinfect",
            {"zone_id": "Z-LEGACY", "completed": True},
            "field-1",
            "field_operator",
            item["version"],
        )
        item = service.act(
            item["id"],
            "sample",
            {"sample_id": "LEGACY-S", "zone_id": "Z-LEGACY", "concentration": 1},
            "lab-1",
            "lab",
            item["version"],
        )
        item = service.act(
            item["id"],
            "restore",
            {"all_zones_cleared": True},
            "coord-1",
            "coordinator",
            item["version"],
        )
        self.assertEqual(item["status"], "restored")


if __name__ == "__main__":
    unittest.main()
