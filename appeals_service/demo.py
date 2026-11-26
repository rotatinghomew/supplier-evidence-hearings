"""可重复运行的内存业务演示：种子数据 -> 裁决 -> 实物回执 -> 送达范围纠正。"""

from __future__ import annotations

from .seed import seed_service
from .service import AppealsService

AT = "2026-12-04T09:00:00+08:00"


def run_demo(state_path: str | None = None) -> dict:
    svc = seed_service(AppealsService(state_path) if state_path else AppealsService())
    cid = "CASE-ORDER-73"

    lp_dg19 = svc.delivery_view(cid, "DG19")
    lp_dg20 = svc.delivery_view(cid, "DG20")
    yy_dg22 = svc.delivery_view(cid, "DG22")

    decision_lp = svc.make_decision(
        cid,
        {
            "decision_id": "DEC-LP-SEAL",
            "at": AT,
            "summary": "封签事项期限已满；同车装载事实因遮盖件披露撤回而排除出龙浦裁决依据。",
            "items": [
                {
                    "matter_id": "L1:seal_check",
                    "party": "CO-LP",
                    "outcome": "uphold_reject",
                    "cited_materials": [["G2", 1]],
                    "independently_executable": True,
                    "execution": {"action": "transfer_for_processing", "quantity": 120, "unit": "筐"},
                }
            ],
        },
        actor_id="REV-01",
    )
    decision_yy = svc.make_decision(
        cid,
        {
            "decision_id": "DEC-YY-SPLIT",
            "at": AT,
            "items": [
                {
                    "matter_id": "L2:split_time",
                    "party": "CO-YY",
                    "outcome": "uphold_reject",
                    "cited_materials": [["G1", 3]],
                    "independently_executable": True,
                    "execution": {"action": "hold_and_transfer", "quantity": 80, "unit": "筐"},
                }
            ],
        },
        actor_id="REV-01",
    )

    item_id = decision_lp["items"][0]["id"]
    execution = svc.create_execution(
        cid,
        {
            "decision_id": decision_lp["id"],
            "item_id": item_id,
            "action": "transfer_for_processing",
            "quantity": 120,
            "unit": "筐",
            "carrier": "合成运输01",
            "at": "2026-12-04T10:00:00+08:00",
        },
        actor_id="WH-01",
    )
    receipt = svc.record_physical_receipt(
        cid,
        execution["id"],
        {
            "id": "PR-SEAL-001",
            "kind": "transfer_receipt",
            "quantity": 120,
            "received_by": "处置场签收人",
            "location": "D-处置线",
            "at": "2026-12-04T11:00:00+08:00",
        },
        actor_id="WH-01",
    )

    warehouse_before_correction = svc.warehouse_view("WH-01", at=AT)
    correction = svc.correct_service_scope(
        cid,
        {
            "correction_id": "COR-DEMO",
            "at": "2026-12-04T14:00:00+08:00",
            "description": "复核后确认 DG19 虽有物理交付回执 Z2，但 LP-01 代表资格已于 13:10 到期；核对后维持 DG20 为有效送达，并演示已执行部分转补救。",
            "targets": [{"decision_id": decision_lp["id"], "item_id": item_id}],
        },
        actor_id="CLERK-01",
    )
    warehouse_after_correction = svc.warehouse_view("WH-01", at="2026-12-04T14:01:00+08:00")

    lp_served = svc.served_and_openable(cid, actor_id="LP-02", at=AT)
    lp_view = svc.party_view(cid, "CO-LP", AT, actor=svc.actor("LP-02"))

    return {
        "delivery_recognition": {
            "DG19_current_carrier_state": lp_dg19["carrier_state"],
            "DG19_effective": lp_dg19["effective"],
            "Z1_preserved": any(r["id"] == "Z1" for r in lp_dg19["receipts"]),
            "Z2_recognizes_bundle": lp_dg19["receipts"][-1]["evaluation"]["recognized_original_bundle"],
            "DG20_effective": lp_dg20["effective"],
            "DG22_effective": yy_dg22["effective"],
        },
        "lp_served_and_openable": lp_served,
        "lp_matters": [
            {
                "id": m["id"],
                "basis_available": m["basis_available"],
                "procedure_state": m["procedure"]["state"],
                "usable_materials": m["usable_materials"],
            }
            for m in lp_view["matters"]
        ],
        "decisions": [decision_lp["id"], decision_yy["id"]],
        "physical_receipt": receipt,
        "warehouse_before_correction_pending_count": len(
            warehouse_before_correction["enforceable_rejections"]
        ),
        "correction": correction,
        "warehouse_after_correction": {
            "enforceable_rejections": warehouse_after_correction["enforceable_rejections"],
            "executed_items": warehouse_after_correction["executed_items"],
        },
        "remedies": svc.list_remedies("REMEDY-01"),
    }


if __name__ == "__main__":
    import json

    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
