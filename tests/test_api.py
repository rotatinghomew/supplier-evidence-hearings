from __future__ import annotations

import json
from pathlib import Path
import sys
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from appeals_service.api import create_handler  # noqa: E402
from appeals_service.seed import seed_service  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = seed_service()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(cls.svc))
        cls.port = cls.httpd.server_address[1]
        cls.thread = Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, body=None, actor=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if actor:
            req.add_header("X-Actor", actor)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health_and_scoped_views(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

        status, lp = self.request(
            "GET", "/api/cases/CASE-ORDER-73?at=2026-12-04T09:00:00%2B08:00", actor="LP-02"
        )
        self.assertEqual(status, 200)
        self.assertEqual(lp["org"], "CO-LP")
        self.assertFalse(any(m["ref"] == ["G1", 1] for m in lp["materials"]))

        status, _ = self.request(
            "GET",
            "/api/cases/CASE-ORDER-73/materials/G1/versions/1?at=2026-12-04T09:00:00%2B08:00",
            actor="LP-02",
        )
        self.assertEqual(status, 403)

        status, verification = self.request("GET", "/api/cases/CASE-ORDER-73/verification", actor="CLERK-01")
        self.assertEqual(status, 200)
        self.assertIn("proof_map", verification)

        status, warehouse = self.request("GET", "/api/warehouse", actor="WH-01")
        self.assertEqual(status, 200)
        self.assertIn("enforceable_rejections", warehouse)

    def test_post_decision_conflict_is_json(self):
        status, body = self.request(
            "POST",
            "/api/cases/CASE-ORDER-73/decisions",
            {
                "actor": "REV-01",
                "at": "2026-12-03T15:00:00+08:00",
                "items": [{
                    "matter_id": "L1:seal_check",
                    "outcome": "uphold_reject",
                    "cited_materials": [["G2", 1]],
                    "independently_executable": True,
                }],
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "conflict")


if __name__ == "__main__":
    unittest.main()
