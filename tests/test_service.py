from __future__ import annotations

import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from appeals_service.common import ConflictError, PermissionDeniedError  # noqa: E402
from appeals_service.seed import seed_service  # noqa: E402
from appeals_service.service import AppealsService  # noqa: E402


class SeedScenarioTest(unittest.TestCase):
    def setUp(self):
        self.svc = seed_service(AppealsService())
        self.cid = "CASE-ORDER-73"
        self.at = "2026-12-04T09:00:00+08:00"

    def test_unknown_receipt_and_late_expired_agent_do_not_start_deadline(self):
        dg19 = self.svc.delivery_view(self.cid, "DG19")
        z1 = next(r for r in dg19["receipts"] if r["id"] == "Z1")
        z2 = next(r for r in dg19["receipts"] if r["id"] == "Z2")

        self.assertEqual(z1["state"], "unknown")
        self.assertFalse(z1["evaluation"]["effective"])
        self.assertIn("carrier_status_unknown", z1["evaluation"]["reasons"])
        self.assertTrue(z2["evaluation"]["physical_delivered"])
        self.assertFalse(z2["evaluation"]["effective"])
        self.assertIn("recipient_not_authorized_at_delivery", z2["evaluation"]["reasons"])
        self.assertEqual(z2["evaluation"]["recognized_original_bundle"], "DG19")

        checked = self.svc.record_timeout_check(
            self.cid, "DG19", {"at": "2026-12-03T14:00:00+08:00"}, "CLERK-01"
        )
        self.assertFalse(checked["definite_nondelivery"])
        self.assertEqual(checked["carrier_state"], "delivered")

        # 只有 DG20 给 CO-LP 的两个事项产生期限。
        lp_deadlines = [d for d in self.svc._case(self.cid)["deadlines"] if d["org"] == "CO-LP"]
        self.assertEqual({d["receipt_id"] for d in lp_deadlines}, {"Z3"})
        self.assertEqual(len(lp_deadlines), 2)
        self.assertTrue(all(d["due_at"] == "2026-12-04T02:34:00+08:00" for d in lp_deadlines))

    def test_upper_representative_bound_is_exclusive(self):
        case = self.svc._case(self.cid)
        self.assertIsNotNone(
            self.svc.active_mandate(case, "CO-LP", "LP-01", "2026-12-03T13:09:59+08:00")
        )
        self.assertIsNone(
            self.svc.active_mandate(case, "CO-LP", "LP-01", "2026-12-03T13:10:00+08:00")
        )
        self.assertIsNotNone(
            self.svc.active_mandate(case, "CO-LP", "LP-02", "2026-12-03T13:10:00+08:00")
        )

    def test_withdrawal_separates_served_from_openable_and_party_basis(self):
        index = self.svc.served_and_openable(self.cid, actor_id="LP-02", at=self.at)
        g1r2 = next(x for x in index["openable"] if x["ref"] == ["G1", 2])
        self.assertFalse(g1r2["openable"])
        self.assertIn("consent_withdrawn", g1r2["reasons"])

        served_g1r2 = [
            x for x in index["served"] if x["ref"] == ["G1", 2] and x["effective_service"]
        ]
        self.assertEqual(len(served_g1r2), 1)
        self.assertEqual(served_g1r2[0]["effective_service"]["bundle_id"], "DG20")

        # 龙浦不能打开岩源原件，因此看不到同页价格。
        with self.assertRaises(PermissionDeniedError):
            self.svc.get_material_content(self.cid, "G1", 1, "LP-02", self.at)
        redacted = self.svc.get_material_content(self.cid, "G1", 2, "LP-02", "2026-12-03T14:00:00+08:00")
        self.assertNotIn("780", redacted["content"])

        lp_co = self.svc.usable_materials(self.cid, "L1:co_loading", "CO-LP", self.at)
        yy_co = self.svc.usable_materials(self.cid, "L2:co_loading", "CO-YY", self.at)
        self.assertEqual(lp_co, [])
        self.assertTrue(any(x["ref"] == ["G1", 1] for x in yy_co))
        remands = self.svc.list_remands(self.cid, "CLERK-01")
        lp_remand = next(r for r in remands if r["party"] == "CO-LP" and r["matter_id"] == "L1:co_loading")
        self.assertEqual(lp_remand["status"], "returned_for_substitute")

    def test_decision_gate_and_party_specific_exclusion(self):
        with self.assertRaises(ConflictError) as ctx:
            self.svc.make_decision(
                self.cid,
                {
                    "decision_id": "TOO-EARLY",
                    "at": "2026-12-03T15:00:00+08:00",
                    "items": [
                        {
                            "matter_id": "L1:seal_check",
                            "outcome": "uphold_reject",
                            "cited_materials": [["G2", 1]],
                            "independently_executable": True,
                        }
                    ],
                },
                "REV-01",
            )
        self.assertEqual(ctx.exception.details["procedure"]["state"], "open")

        with self.assertRaises(ConflictError):
            self.svc.make_decision(
                self.cid,
                {
                    "decision_id": "BAD-CO",
                    "at": self.at,
                    "items": [
                        {
                            "matter_id": "L1:co_loading",
                            "outcome": "uphold_reject",
                            "cited_materials": [["G1", 2]],
                            "independently_executable": False,
                        }
                    ],
                },
                "REV-01",
            )

        decision = self.svc.make_decision(
            self.cid,
            {
                "decision_id": "DEC-SEAL",
                "at": self.at,
                "items": [
                    {
                        "matter_id": "L1:seal_check",
                        "outcome": "uphold_reject",
                        "cited_materials": [["G2", 1]],
                        "independently_executable": True,
                    }
                ],
            },
            "REV-01",
        )
        self.assertEqual(decision["items"][0]["execution_status"], "pending_execution")
        warehouse = self.svc.warehouse_view("WH-01", at=self.at)
        self.assertTrue(
            any(x["item_id"] == decision["items"][0]["id"] for x in warehouse["enforceable_rejections"])
        )


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.svc = AppealsService()
        self.svc.register_org("FAC", "厂方", "factory")
        self.svc.register_org("SUP", "测试合作社", "supplier")
        self.svc.register_actor("CLERK", "FAC", roles=["clerk"])
        self.svc.register_actor("REV", "FAC", roles=["reviewer"])
        self.svc.register_actor("WH", "FAC", roles=["warehouse"])
        self.svc.register_actor("S1", "SUP", roles=["supplier"])
        self.t0 = "2026-12-01T08:00:00+08:00"
        self.t1 = "2026-12-01T10:00:00+08:00"
        self.cid = "C1"
        self.svc.create_case(
            {
                "case_id": self.cid,
                "purchase": "PO-1",
                "rejection_at": self.t0,
                "subjects": [
                    {"id": "S", "supplier": "SUP", "batch": "B1", "facts": ["a", "b"]}
                ],
            },
            "CLERK",
        )
        self.svc.add_mandate(self.cid, {"org": "SUP", "actor": "S1"}, "CLERK")
        self.svc.file_appeal(
            self.cid,
            {"id": "REQ1", "subject": "S", "by": "S1", "targets": ["a", "b"], "at": "2026-12-01T09:00:00+08:00"},
            "S1",
        )
        self.svc.accept_case(self.cid, {"at": "2026-12-01T09:10:00+08:00"}, "CLERK")
        for mid, fact in [("A", "a"), ("B", "b")]:
            self.svc.upload_material(
                self.cid,
                {
                    "id": mid,
                    "rev": 1,
                    "owner": "FAC",
                    "kind": "report",
                    "content": f"proof {fact}",
                    "facts": [fact],
                    "allows": ["SUP", "FAC"],
                },
                "CLERK",
            )

    def _deliver(self, bid, refs, at, **extra):
        self.svc.create_delivery(
            self.cid,
            {
                "bundle_id": bid,
                "org": "SUP",
                "person": "S1",
                "docs": refs,
                "send": True,
                "sent_at": at,
                **extra,
            },
            "CLERK",
        )
        return self.svc.ingest_receipt(
            self.cid,
            {"id": "R-" + bid, "bundle": bid, "person": "S1", "state": "delivered", "at": at, "docs": refs},
        )

    def test_supplement_for_one_item_keeps_unrelated_deadline(self):
        self._deliver("B1", [["A", 1]], self.t1, matter_ids=["S:a"])
        old = self.svc.current_deadline(self.svc._case(self.cid), "S:a", "SUP")
        self.assertEqual(old["due_at"], "2026-12-01T22:00:00+08:00")

        t2 = "2026-12-01T12:00:00+08:00"
        self._deliver("B2", [["B", 1]], t2, kind="supplement", matter_ids=["S:b"])
        after_a = self.svc.current_deadline(self.svc._case(self.cid), "S:a", "SUP")
        b = self.svc.current_deadline(self.svc._case(self.cid), "S:b", "SUP")
        self.assertEqual(after_a["id"], old["id"])
        self.assertEqual(after_a["due_at"], old["due_at"])
        self.assertEqual(b["due_at"], "2026-12-02T00:00:00+08:00")

    def test_defense_changing_version_restarts_only_that_matter_and_preserves_old_answer(self):
        self._deliver("B1", [["A", 1]], self.t1, matter_ids=["S:a"])
        self.svc.file_answer(
            self.cid,
            {"id": "ANS-OLD", "at": "2026-12-01T11:00:00+08:00",
             "items": [{"matter_id": "S:a", "text": "旧版答辩"}]},
            "S1",
        )
        self.svc.upload_material(
            self.cid,
            {
                "id": "A", "rev": 2, "owner": "FAC", "kind": "report",
                "content": "changed proof a with new quantity", "facts": ["a"],
                "allows": ["SUP", "FAC"], "defense_change": True,
            },
            "CLERK",
        )
        t2 = "2026-12-01T13:00:00+08:00"
        self._deliver(
            "B2", [["A", 2]], t2, kind="new_version",
            matter_ids=["S:a"], changed_matters=["S:a"],
        )
        case = self.svc._case(self.cid)
        current = self.svc.current_deadline(case, "S:a", "SUP")
        self.assertEqual(current["generation"], 2)
        self.assertEqual(current["due_at"], "2026-12-02T01:00:00+08:00")
        early = self.svc.deadline_state(case, current, "2026-12-01T13:30:00+08:00")
        self.assertEqual(early["state"], "open")

        # 晚到的旧版回执不能生成 gen3/新版期限。
        self.svc.ingest_receipt(
            self.cid,
            {"id": "R-OLD-LATE", "bundle": "B-OLD-LATE", "person": "S1",
             "state": "delivered", "at": "2026-12-01T15:00:00+08:00", "docs": [["A", 1]]},
        )
        current2 = self.svc.current_deadline(self.svc._case(self.cid), "S:a", "SUP")
        self.assertEqual(current2["generation"], 2)

        self.svc.file_answer(
            self.cid,
            {"id": "ANS-NEW", "at": "2026-12-01T14:00:00+08:00",
             "items": [{"matter_id": "S:a", "text": "新版答辩"}]},
            "S1",
        )
        state = self.svc.deadline_state(self.svc._case(self.cid), current2, "2026-12-01T14:01:00+08:00")
        self.assertEqual(state["state"], "answered")
        content_old = self.svc.get_material_content(self.cid, "A", 1, "S1", t2)
        self.assertIn("proof a", content_old["content"])
        self.assertEqual(len(self.svc._case(self.cid)["answers"]), 2)

    def test_early_lawful_answer_or_waiver_allows_decision_before_expiry(self):
        self._deliver("B-ANS", [["A", 1]], self.t1, matter_ids=["S:a"])
        self.svc.file_answer(
            self.cid,
            {"id": "ANS-EARLY", "at": "2026-12-01T11:00:00+08:00",
             "items": [{"matter_id": "S:a", "text": "已充分答辩"}]},
            "S1",
        )
        decision = self.svc.make_decision(
            self.cid,
            {"decision_id": "D-EARLY", "at": "2026-12-01T11:30:00+08:00", "items": [{
                "matter_id": "S:a", "outcome": "restore_receipt",
                "independently_executable": True,
            }]},
            "REV",
        )
        self.assertEqual(decision["items"][0]["procedure_snapshot"]["state"], "answered")

        self._deliver("B-WV", [["B", 1]], self.t1, matter_ids=["S:b"])
        self.svc.waive_period(
            self.cid,
            {"at": "2026-12-01T10:30:00+08:00", "deadline_id": self.svc.current_deadline(
                self.svc._case(self.cid), "S:b", "SUP"
            )["id"]},
            "S1",
        )
        waived = self.svc.make_decision(
            self.cid,
            {"decision_id": "D-WV", "at": "2026-12-01T10:40:00+08:00", "items": [{
                "matter_id": "S:b", "outcome": "supplement_investigation",
            }]},
            "REV",
        )
        self.assertEqual(waived["items"][0]["procedure_snapshot"]["state"], "waived")

    def test_queued_material_stops_on_withdrawal_but_unknown_is_not_undelivered(self):
        self.svc.create_delivery(
            self.cid,
            {"bundle_id": "BQ", "org": "SUP", "person": "S1", "docs": [["A", 1]]},
            "CLERK",
        )
        wd = self.svc.withdraw_consent(
            self.cid,
            {"id": "W1", "material": "A", "rev": 1, "party": "SUP", "at": "2026-12-01T11:00:00+08:00"},
            "CLERK",
        )
        self.assertTrue(wd["stopped_items"])
        bundle = self.svc.delivery_view(self.cid, "BQ")
        self.assertEqual(bundle["items"][0]["status"], "stopped_before_delivery")

        self.svc.ingest_receipt(
            self.cid,
            {"id": "RU", "bundle": "BU", "person": "S1", "state": "unknown",
             "at": "2026-12-01T11:30:00+08:00", "docs": [["A", 1]]},
        )
        timeout = self.svc.record_timeout_check(self.cid, "BU", {"at": "2026-12-01T18:00:00+08:00"}, "CLERK")
        self.assertEqual(timeout["carrier_state"], "unknown")
        self.assertFalse(timeout["definite_nondelivery"])

    def test_executed_correction_creates_remedy_and_cannot_relist(self):
        self._deliver("B1", [["A", 1]], self.t1, matter_ids=["S:a"])
        decision = self.svc.make_decision(
            self.cid,
            {
                "decision_id": "D1",
                "at": "2026-12-01T23:00:00+08:00",
                "items": [{
                    "matter_id": "S:a", "outcome": "uphold_reject",
                    "cited_materials": [["A", 1]], "independently_executable": True,
                }],
            },
            "REV",
        )
        item_id = decision["items"][0]["id"]
        ex = self.svc.create_execution(
            self.cid,
            {"decision_id": "D1", "item_id": item_id, "quantity": 10},
            "WH",
        )
        self.svc.record_physical_receipt(self.cid, ex["id"], {"id": "PR1", "at": "2026-12-02T01:00:00+08:00"}, "WH")
        correction = self.svc.correct_service_scope(
            self.cid,
            {"id": "C1-COR", "at": "2026-12-02T02:00:00+08:00",
             "targets": [{"decision_id": "D1", "item_id": item_id}]},
            "CLERK",
        )
        self.assertEqual(correction["results"][0]["action"], "executed_part_referred_to_remedy")
        with self.assertRaises(ConflictError):
            self.svc.create_execution(
                self.cid, {"decision_id": "D1", "item_id": item_id}, "WH"
            )
        view = self.svc.warehouse_view("WH")
        self.assertFalse(any(x["item_id"] == item_id for x in view["enforceable_rejections"]))
        self.assertTrue(any(x["item_id"] == item_id for x in view["executed_items"]))
        self.assertEqual(len(self.svc.list_remedies("CLERK")), 1)

    def test_unexecuted_correction_blocks_pending_item(self):
        self._deliver("B1", [["A", 1]], self.t1, matter_ids=["S:a"])
        self.svc.make_decision(
            self.cid,
            {"decision_id": "D2", "at": "2026-12-01T23:00:00+08:00", "items": [{
                "matter_id": "S:a", "outcome": "uphold_reject",
                "cited_materials": [["A", 1]], "independently_executable": True,
            }]},
            "REV",
        )
        item_id = "D2:I1"
        self.svc.correct_service_scope(
            self.cid,
            {"at": "2026-12-02T02:00:00+08:00", "decision_id": "D2", "item_ids": [item_id]},
            "CLERK",
        )
        with self.assertRaises(ConflictError):
            self.svc.create_execution(
                self.cid, {"decision_id": "D2", "item_id": item_id}, "WH"
            )


if __name__ == "__main__":
    unittest.main()
