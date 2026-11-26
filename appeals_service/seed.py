"""从仓库 inputs/ 与 evidence/ 构造合成示范服务。"""

from __future__ import annotations

from pathlib import Path
import json

from .service import AppealsService

ROOT = Path(__file__).resolve().parents[1]


def load_json(name: str):
    return json.loads((ROOT / "inputs" / f"{name}.json").read_text(encoding="utf-8"))


def seed_service(service: AppealsService | None = None) -> AppealsService:
    service = service or AppealsService()

    service.register_org("FAC", "厂方", "factory")
    service.register_org("CO-LP", "龙浦合作社", "supplier")
    service.register_org("CO-YY", "岩源合作社", "supplier")

    service.register_actor("FAC-01", "FAC", roles=["factory"], name="厂方报收员")
    service.register_actor("CLERK-01", "FAC", roles=["clerk"], name="异议经办人")
    service.register_actor("REV-01", "FAC", roles=["reviewer"], name="复核人")
    service.register_actor("WH-01", "FAC", roles=["warehouse"], name="仓班")
    service.register_actor("REMEDY-01", "FAC", roles=["remedy_authority", "clerk"], name="补救有权经办人")
    service.register_actor("DELIVERY-01", "FAC", roles=["delivery"], name="送达回传通道")
    service.register_actor("LP-01", "CO-LP", roles=["supplier"])
    service.register_actor("LP-02", "CO-LP", roles=["supplier"])
    service.register_actor("YY-09", "CO-YY", roles=["supplier"])

    appeals = load_json("appeals")
    case_id = "CASE-ORDER-73"
    if case_id not in service.state["cases"]:
        service.create_case(
            {
                "case_id": case_id,
                "purchase": appeals["purchase"],
                "rejection_at": appeals["rejection_at"],
                "appeal_hours": appeals["appeal_hours"],
                "files_due_hours": appeals["files_due_hours"],
                "subjects": appeals["subjects"],
                "report": {
                    "original_decision": "reject",
                    "original_decision_text": "厂方原判：拒收",
                    "agreement": {"ref": appeals["purchase"]},
                    "proposed_disposition": {"type": "refuse", "source": "subjects[].proposed"},
                },
            },
            actor_id="FAC-01",
        )

    for agent in appeals.get("agents", []):
        mandate_id = "M-" + agent["actor"]
        if not any(m["id"] == mandate_id for m in service._case(case_id)["mandates"]):
            service.add_mandate(
                case_id,
                {
                    "id": mandate_id,
                    "org": agent["org"],
                    "actor": agent["actor"],
                    "valid_from": agent.get("valid_from"),
                    "valid_until": agent.get("valid_until"),
                    "doc_groups": ["*"],
                },
                actor_id="CLERK-01",
            )

    for req in appeals.get("requests", []):
        if not any(r["id"] == req["id"] for r in service._case(case_id)["requests"]):
            service.file_appeal(case_id, req, actor_id=req["by"])

    if service._case(case_id)["status"] not in {"accepted", "decided_or_partial"}:
        service.accept_case(
            case_id,
            {
                "at": "2026-12-03T11:00:00+08:00",
                "matter_configs": [
                    {
                        "subject_id": "L1",
                        "fact": "seal_check",
                        "independently_executable": True,
                        "label": "封签检查",
                    },
                    {
                        "subject_id": "L1",
                        "fact": "co_loading",
                        "independently_executable": False,
                        "label": "同车装载",
                    },
                    {
                        "subject_id": "L2",
                        "fact": "split_time",
                        "independently_executable": True,
                        "label": "分货时间",
                    },
                    {
                        "subject_id": "L2",
                        "fact": "co_loading",
                        "independently_executable": False,
                        "label": "同车装载",
                    },
                ],
            },
            actor_id="CLERK-01",
        )

    disclosure = load_json("disclosure")
    kind_by_file = {
        "g1-master.txt": "original",
        "g1-cover.txt": "redacted",
        "g1-extract.txt": "excerpt",
        "g2-report.txt": "inspection_report",
    }
    for row in disclosure["materials"]:
        mid, rev = row["id"], int(row["rev"])
        try:
            service._material_rev(service._case(case_id), mid, rev)
            continue
        except Exception:
            pass
        path = ROOT / "evidence" / row["file"]
        needs_confirm = kind_by_file.get(row["file"]) in {"redacted", "excerpt"}
        service.upload_material(
            case_id,
            {
                "id": mid,
                "rev": rev,
                "owner": row["owner"],
                "kind": kind_by_file.get(row["file"], "document"),
                "source_file": str(path),
                "source_path": f"evidence/{row['file']}",
                "facts": row["facts"],
                "allows": row["allows"],
                "confirmed": needs_confirm,
                "confirmation": (
                    {
                        "by": row["owner"],
                        "reference": f"CONFIRM-{mid}-R{rev}",
                        "note": "合成样例：证据方确认有效的遮盖件/经同意摘录",
                    }
                    if needs_confirm
                    else None
                ),
                "at": "2026-12-03T11:20:00+08:00",
            },
            actor_id="CLERK-01",
        )

    delivery = load_json("delivery")
    for receipt in delivery:
        if receipt["id"] not in service._case(case_id)["receipts"]:
            service.ingest_receipt(case_id, receipt, actor_id="DELIVERY-01")

    for withdrawal in disclosure.get("withdrawals", []):
        if not any(w["id"] == withdrawal["id"] for w in service._case(case_id)["withdrawals"]):
            service.withdraw_consent(case_id, withdrawal, actor_id="CLERK-01")

    return service


if __name__ == "__main__":
    svc = seed_service()
    case = svc._case("CASE-ORDER-73")
    print(json.dumps({
        "case_id": "CASE-ORDER-73",
        "status": case["status"],
        "bundles": list(case["bundles"]),
        "deadlines": svc._deadline_summaries(case, svc._now("2026-12-04T09:00:00+08:00")),
        "lp_view": {
            "matters": [
                {k: m[k] for k in ("id", "basis_available", "procedure") if k in m}
                for m in svc.party_view("CASE-ORDER-73", "CO-LP", "2026-12-04T09:00:00+08:00")["matters"]
            ]
        },
    }, ensure_ascii=False, indent=2))
