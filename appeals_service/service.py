from __future__ import annotations

from datetime import timedelta
import hashlib
from typing import Any

from .common import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ServiceError,
    Store,
    ValidationError,
    as_ref,
    iso,
    parse_dt,
    refs_key,
)

STAFF_ROLES = {"clerk", "reviewer", "warehouse", "factory", "remedy_authority", "delivery"}
SUPPLIER_ROLE = "supplier"
FINAL_PROCEDURE_STATES = {"answered", "waived", "expired"}
DECISION_OUTCOMES = {"uphold_reject", "restore_receipt", "supplement_investigation"}


class AppealsService:
    """拒收申诉、披露、送达、裁决与执行的事务服务。

    所有写方法都接受可选 ``at``（ISO 字符串或 datetime），以便测试和回放。
    """

    def __init__(self, path: str | None = None, *, clock=None):
        self.store = Store(path)
        self._lock = self.store.lock
        self.clock = clock

    # ---------- 基础工具 ----------

    @property
    def state(self) -> dict[str, Any]:
        return self.store.state

    def _now(self, at: Any = None):
        if at is not None:
            return parse_dt(at, field="at")
        if self.clock is not None:
            value = self.clock()
            return parse_dt(value, field="clock")
        from datetime import datetime, timezone

        return datetime.now(timezone.utc)

    def _save(self) -> None:
        self.store.save()

    def _seq(self, prefix: str) -> str:
        self.state["seq"] = int(self.state.get("seq", 0)) + 1
        return f"{prefix}-{self.state['seq']:06d}"

    def audit(self, action: str, actor: str | None = None, **details: Any) -> None:
        event = {
            "at": iso(self._now(details.pop("at", None))),
            "action": action,
            "actor": actor,
        }
        if details:
            event["details"] = details
        self.state.setdefault("audit", []).append(event)

    def register_org(self, org_id: str, name: str | None = None, kind: str = "party") -> dict:
        with self._lock:
            for org in self.state["orgs"]:
                if org["id"] == org_id:
                    return org
            org = {"id": org_id, "name": name or org_id, "kind": kind}
            self.state["orgs"].append(org)
            self._save()
            return org

    def register_actor(
        self,
        actor_id: str,
        org: str,
        *,
        name: str | None = None,
        roles: list[str] | None = None,
    ) -> dict:
        with self._lock:
            self.register_org(org, kind="organization")
            existing = self.state["actors"].get(actor_id)
            if existing and existing.get("org") != org:
                raise ConflictError(f"人员 {actor_id} 已归属 {existing.get('org')}")
            record = {
                "id": actor_id,
                "org": org,
                "name": name or actor_id,
                "roles": sorted(set(roles or [SUPPLIER_ROLE])),
            }
            self.state["actors"][actor_id] = record
            self._save()
            return record

    def actor(self, actor_id: str | None) -> dict:
        if not actor_id:
            raise PermissionDeniedError("缺少 X-Actor 或 actor")
        record = self.state["actors"].get(actor_id)
        if not record:
            raise PermissionDeniedError(f"未登记人员: {actor_id}")
        return record

    def require_roles(self, actor_id: str | None, *roles: str) -> dict:
        record = self.actor(actor_id)
        if roles and not any(role in record.get("roles", []) for role in roles):
            raise PermissionDeniedError(
                f"人员 {actor_id} 缺少角色 { ' / '.join(roles) }",
                details={"actor": record, "required_any": list(roles)},
            )
        return record

    def _case(self, case_id: str) -> dict:
        case = self.state["cases"].get(case_id)
        if not case:
            raise NotFoundError(f"没有申诉案件: {case_id}")
        return case

    def list_cases(self, actor_id: str | None = None, at: Any = None) -> list[dict]:
        at_dt = self._now(at)
        actor = self.actor(actor_id) if actor_id else None
        result = []
        for case in self.state["cases"].values():
            if actor and not self._is_staff(actor):
                if not self._actor_in_case(case, actor):
                    continue
                result.append(self.party_view(case["id"], actor["org"], at_dt, actor=actor))
            else:
                result.append(self.case_summary(case["id"], at_dt))
        return sorted(result, key=lambda x: x.get("created_at") or "")

    def _is_staff(self, actor: dict) -> bool:
        return bool(set(actor.get("roles", [])) & STAFF_ROLES)

    def _actor_in_case(self, case: dict, actor: dict) -> bool:
        if any(p.get("org") == actor.get("org") for p in case.get("parties", [])):
            return True
        return any(m.get("actor") == actor["id"] for m in case.get("mandates", []))

    def case_summary(self, case_id: str, at: Any = None) -> dict:
        case = self._case(case_id)
        at_dt = self._now(at)
        return {
            "id": case["id"],
            "purchase": case["purchase"],
            "status": case["status"],
            "rejection_at": case["rejection_at"],
            "appeal_deadline": case["appeal_deadline"],
            "created_at": case.get("created_at"),
            "subjects": list(case["subjects"].values()),
            "deadlines": self._deadline_summaries(case, at_dt),
            "decision_ids": [d["id"] for d in case.get("decisions", [])],
        }

    # ---------- 代表资格 ----------

    def add_mandate(self, case_id: str, payload: dict, actor_id: str | None = None) -> dict:
        with self._lock:
            case = self._case(case_id)
            org = payload["org"]
            person = payload["actor"]
            if person not in self.state["actors"]:
                self.register_actor(person, org, roles=[SUPPLIER_ROLE])
            elif self.state["actors"][person]["org"] != org:
                raise ConflictError("人员归属组织与代表授权不一致")
            mandate = {
                "id": payload.get("id") or self._seq("M"),
                "org": org,
                "actor": person,
                "valid_from": iso(parse_dt(payload.get("valid_from"), field="valid_from")),
                "valid_until": iso(parse_dt(payload.get("valid_until"), field="valid_until")),
                "doc_groups": payload.get("doc_groups") or ["*"],
                "scope": payload.get("scope", "appeal_and_files"),
                "note": payload.get("note"),
            }
            case["mandates"].append(mandate)
            self.audit("mandate.added", actor_id, mandate_id=mandate["id"])
            self._save()
            return mandate

    def active_mandate(
        self,
        case: dict,
        org: str,
        person: str,
        at: Any,
        *,
        doc_group: str | None = None,
    ) -> dict | None:
        at_dt = parse_dt(at) or self._now()
        for mandate in case.get("mandates", []):
            if mandate["org"] != org or mandate["actor"] != person:
                continue
            start = parse_dt(mandate.get("valid_from"))
            end = parse_dt(mandate.get("valid_until"))
            if start and at_dt < start:
                continue
            # 明确约定：valid_until 上界不含端点。
            if end and at_dt >= end:
                continue
            groups = mandate.get("doc_groups") or ["*"]
            if doc_group is not None and "*" not in groups and doc_group not in groups:
                continue
            return mandate
        return None

    def require_mandate(
        self,
        case: dict,
        actor: dict,
        at: Any,
        *,
        doc_group: str | None = None,
    ) -> dict:
        mandate = self.active_mandate(
            case, actor["org"], actor["id"], at, doc_group=doc_group
        )
        if not mandate:
            raise PermissionDeniedError(
                f"{actor['id']} 在该时间对本申诉/文件组没有有效代表资格",
                details={"at": iso(parse_dt(at) or self._now()), "doc_group": doc_group},
            )
        return mandate

    # ---------- 厂方报拒收与受理 ----------

    def create_case(self, payload: dict, actor_id: str | None = None) -> dict:
        with self._lock:
            if actor_id:
                actor = self.require_roles(actor_id, "factory", "clerk")
                factory_org = actor["org"] if "factory" in actor.get("roles", []) else payload.get("factory_org", "FAC")
            else:
                factory_org = payload.get("factory_org", "FAC")
            self.register_org(factory_org, "厂方", "factory")

            rejection_dt = parse_dt(payload.get("rejection_at"), field="rejection_at")
            if not rejection_dt:
                raise ValidationError("rejection_at 必填")
            appeal_hours = float(payload.get("appeal_hours", 48))
            files_hours = float(payload.get("files_due_hours", 12))
            subjects_in = payload.get("subjects")
            if not isinstance(subjects_in, list) or not subjects_in:
                raise ValidationError("至少需要一个 subjects")

            case_id = payload.get("case_id") or payload.get("id") or self._seq("CASE")
            if case_id in self.state["cases"]:
                raise ConflictError(f"案件已存在: {case_id}")

            subjects: dict[str, dict] = {}
            parties = [{"org": factory_org, "role": "factory"}]
            for raw in subjects_in:
                sid = raw.get("id") or raw.get("subject_id")
                supplier = raw.get("supplier") or raw.get("supplier_org")
                if not sid or not supplier:
                    raise ValidationError("subject 需要 id 和 supplier")
                self.register_org(supplier, raw.get("supplier_name"), "supplier")
                facts = list(raw.get("facts") or [])
                if not facts:
                    raise ValidationError(f"subject {sid} 至少需要一项事实")
                subject = {
                    "id": sid,
                    "supplier": supplier,
                    "batch": raw.get("batch"),
                    "facts": facts,
                    "proposed": raw.get("proposed", "refuse"),
                    "agreement_clause": raw.get("agreement_clause"),
                    "independently_executable": bool(raw.get("independently_executable", False)),
                }
                subjects[sid] = subject
                if not any(p["org"] == supplier for p in parties):
                    parties.append({"org": supplier, "role": "supplier"})

            report = payload.get("report") or {
                "original_decision": payload.get("original_decision", "reject"),
                "agreement": payload.get("agreement")
                or {"ref": payload.get("purchase"), "terms": payload.get("terms", [])},
                "proposed_disposition": payload.get("proposed_disposition")
                or {"type": "refuse", "proposed": "refuse"},
                "note": payload.get("note"),
            }
            case = {
                "id": case_id,
                "schema": "rejection-appeal/v1",
                "purchase": payload.get("purchase"),
                "factory_org": factory_org,
                "status": "reported",
                "created_at": iso(self._now(payload.get("created_at"))),
                "rejection_at": iso(rejection_dt),
                "appeal_hours": appeal_hours,
                "files_due_hours": files_hours,
                "appeal_deadline": iso(rejection_dt + timedelta(hours=appeal_hours)),
                "report": report,
                "parties": parties,
                "subjects": subjects,
                "requests": [],
                "mandates": [],
                "matters": {},
                "materials": {},
                "withdrawals": [],
                "bundles": {},
                "receipts": {},
                "receipt_order": [],
                "service_events": [],
                "deadlines": [],
                "answers": [],
                "waivers": [],
                "basis_issues": [],
                "remands": [],
                "decisions": [],
                "executions": [],
                "physical_receipts": [],
                "corrections": [],
                "remedies": [],
                "holds": [],
                "investigations": [],
            }
            self.state["cases"][case_id] = case
            self.audit("case.created", actor_id, case_id=case_id)
            self._save()
            return self.case_summary(case_id)

    def file_appeal(self, case_id: str, payload: dict, actor_id: str | None = None) -> dict:
        with self._lock:
            case = self._case(case_id)
            at_dt = self._now(payload.get("at"))
            actor = self.actor(actor_id or payload.get("actor") or payload.get("by"))
            on_behalf = None
            if "clerk" in actor.get("roles", []):
                target_actor = payload.get("by") or payload.get("person")
                if not target_actor:
                    raise ValidationError("经办人代登记申诉必须提供 by")
                target = self.actor(target_actor)
                on_behalf = target["id"]
                self.require_mandate(case, target, at_dt)
                org = target["org"]
            else:
                self.require_mandate(case, actor, at_dt)
                org = actor["org"]

            sid = payload.get("subject") or payload.get("subject_id")
            subject = case["subjects"].get(sid)
            if not subject:
                raise NotFoundError(f"案件 {case_id} 没有 subject {sid}")
            if subject["supplier"] != org:
                raise PermissionDeniedError("只能对本组织供货批次提出异议")
            deadline = parse_dt(case["appeal_deadline"])
            if at_dt > deadline:
                raise ConflictError(
                    "已超过约定申诉期限",
                    details={"appeal_deadline": case["appeal_deadline"], "at": iso(at_dt)},
                )
            targets = list(payload.get("targets") or [])
            unknown = [f for f in targets if f not in subject["facts"]]
            if unknown:
                raise ValidationError(f"异议事实不在厂方报告事实中: {unknown}")
            request = {
                "id": payload.get("id") or self._seq("REQ"),
                "case_id": case_id,
                "subject": sid,
                "by": on_behalf or actor["id"],
                "org": org,
                "targets": targets,
                "statement": payload.get("statement", ""),
                "at": iso(at_dt),
            }
            case["requests"].append(request)
            if case["status"] == "reported":
                case["status"] = "appealed"
            self.audit("appeal.filed", actor["id"], request_id=request["id"])
            self._save()
            return request

    def accept_case(self, case_id: str, payload: dict | None = None, actor_id: str | None = None) -> dict:
        with self._lock:
            payload = payload or {}
            actor = self.require_roles(actor_id or payload.get("actor"), "clerk")
            case = self._case(case_id)
            at_dt = self._now(payload.get("at"))
            configs = {
                (c.get("subject_id"), c.get("fact")): c
                for c in payload.get("matter_configs", [])
            }

            for sid, subject in case["subjects"].items():
                contested = {
                    f
                    for req in case["requests"]
                    if req["subject"] == sid
                    for f in req.get("targets", [])
                }
                for fact in subject["facts"]:
                    mid = payload.get("matter_id") if len(subject["facts"]) == 1 else f"{sid}:{fact}"
                    mid = configs.get((sid, fact), {}).get("id") or f"{sid}:{fact}"
                    cfg = configs.get((sid, fact), {})
                    matter = case["matters"].get(mid) or {
                        "id": mid,
                        "subject_id": sid,
                        "fact": fact,
                        "party": subject["supplier"],
                        "affected_parties": [subject["supplier"]],
                        "contested": fact in contested,
                        "status": "open",
                        "created_at": iso(at_dt),
                    }
                    matter.update(
                        {
                            "label": cfg.get("label", matter.get("label")),
                            "independently_reviewable": cfg.get(
                                "independently_reviewable", True
                            ),
                            "independently_executable": cfg.get(
                                "independently_executable",
                                subject.get("independently_executable", False),
                            ),
                        }
                    )
                    if cfg.get("affected_parties"):
                        matter["affected_parties"] = cfg["affected_parties"]
                    case["matters"][mid] = matter

                hold_id = f"HOLD-{sid}"
                if not any(h.get("id") == hold_id for h in case["holds"]):
                    case["holds"].append(
                        {
                            "id": hold_id,
                            "subject_id": sid,
                            "batch": subject["batch"],
                            "supplier": subject["supplier"],
                            "scope": payload.get("hold_scope", "disputed_goods"),
                            "status": "held",
                            "at": iso(at_dt),
                            "reason": "申诉受理后争议范围内货物保留",
                        }
                    )

            case["status"] = "accepted"
            case["accepted_at"] = iso(at_dt)
            self.audit("case.accepted", actor["id"], case_id=case_id)
            self._save()
            return self.party_view(case_id, actor["org"], at_dt, actor=actor)

    def configure_matter(self, case_id: str, matter_id: str, payload: dict, actor_id: str | None = None) -> dict:
        with self._lock:
            self.require_roles(actor_id or payload.get("actor"), "clerk")
            case = self._case(case_id)
            matter = case["matters"].get(matter_id)
            if not matter:
                raise NotFoundError(f"没有事项 {matter_id}")
            for key in ("label", "independently_reviewable", "independently_executable", "note"):
                if key in payload:
                    matter[key] = payload[key]
            if payload.get("affected_parties") is not None:
                parties = list(payload["affected_parties"])
                if matter.get("party") and matter["party"] not in parties:
                    parties.append(matter["party"])
                matter["affected_parties"] = list(dict.fromkeys(parties))
                for org in parties:
                    if not any(p["org"] == org for p in case["parties"]):
                        self.register_org(org, kind="interested_party")
                        case["parties"].append({"org": org, "role": "interested_party"})
            self.audit("matter.configured", actor_id, matter_id=matter_id)
            self._save()
            return matter

    # ---------- 证据版本、归属、披露 ----------

    def _material_rev(self, case: dict, material_id: str, rev: int) -> dict:
        group = case["materials"].get(str(material_id))
        if not group:
            raise NotFoundError(f"没有材料 {material_id}")
        for item in group["revisions"]:
            if int(item["rev"]) == int(rev):
                return item
        raise NotFoundError(f"没有材料版本 {material_id} r{rev}")

    def _find_ref(self, case: dict, ref: Any) -> dict:
        mid, rev = as_ref(ref)
        return self._material_rev(case, mid, int(rev))

    def upload_material(self, case_id: str, payload: dict, actor_id: str | None = None) -> dict:
        with self._lock:
            actor = self.actor(actor_id or payload.get("actor"))
            case = self._case(case_id)
            owner = payload.get("owner") or actor.get("org")
            if not owner:
                raise ValidationError("材料必须有 owner")
            if not (
                "clerk" in actor.get("roles", [])
                or "reviewer" in actor.get("roles", [])
                or actor.get("org") == owner
            ):
                raise PermissionDeniedError("只能由经办人/复核人或证据归属方上传")

            mid = str(payload.get("id") or payload.get("material_id") or self._seq("MAT"))
            at_dt = self._now(payload.get("at") or payload.get("created_at"))
            group = case["materials"].setdefault(mid, {"id": mid, "revisions": []})
            existing_revs = [int(x["rev"]) for x in group["revisions"]]
            rev = int(payload.get("rev") or (max(existing_revs) + 1 if existing_revs else 1))
            if rev in existing_revs:
                raise ConflictError(f"材料版本已存在且不可覆盖: {mid} r{rev}")

            content = payload.get("content")
            source_path = payload.get("source_path") or payload.get("file")
            if content is None and payload.get("source_file"):
                from pathlib import Path

                path = Path(payload["source_file"])
                if not path.is_absolute():
                    path = Path.cwd() / path
                content = path.read_text(encoding="utf-8")
            if content is None:
                content = ""
            kind = payload.get("kind", "original")
            needs_confirm = kind in {"redacted", "cover", "excerpt", "extract"}
            confirmation = payload.get("confirmation")
            confirmed = bool(payload.get("confirmed") or confirmation or payload.get("consent"))
            material = {
                "id": mid,
                "rev": rev,
                "ref": [mid, rev],
                "owner": owner,
                "kind": kind,
                "source_file": source_path,
                "content": content,
                "sha256": hashlib.sha256(str(content).encode("utf-8")).hexdigest(),
                "facts": list(payload.get("facts") or []),
                "allows": list(payload.get("allows") or ([owner] if owner else [])),
                "confirmed": confirmed if not needs_confirm else confirmed,
                "confirmation": confirmation,
                "defense_change": bool(payload.get("defense_change", False)),
                "changed_facts": list(payload.get("changed_facts") or []),
                "created_at": iso(at_dt),
                "created_by": actor["id"],
            }
            if not material["facts"]:
                raise ValidationError("材料必须标明可证明的 facts")
            group["revisions"].append(material)
            group["revisions"].sort(key=lambda x: int(x["rev"]))
            self.audit("material.uploaded", actor["id"], material=f"{mid}r{rev}")
            self._refresh_basis_issues(case, self._now(payload.get("at")))
            self._save()
            return self._public_material(case, material, actor["org"], staff=self._is_staff(actor), at=at_dt)

    def confirm_material(
        self, case_id: str, material_id: str, rev: int, payload: dict | None = None, actor_id: str | None = None
    ) -> dict:
        with self._lock:
            payload = payload or {}
            actor = self.actor(actor_id or payload.get("actor"))
            case = self._case(case_id)
            material = self._material_rev(case, material_id, int(rev))
            at_dt = self._now(payload.get("at"))
            if not ("clerk" in actor["roles"] or actor["org"] == material["owner"]):
                raise PermissionDeniedError("遮盖件/摘录须由证据方确认，或由经办人登记确认凭据")
            material["confirmed"] = True
            material["confirmation"] = payload.get("confirmation") or {
                "confirmed_by": actor["id"],
                "owner": material["owner"],
                "at": iso(at_dt),
                "reference": payload.get("reference"),
            }
            if payload.get("allows") is not None:
                material["allows"] = list(payload["allows"])
            self.audit("material.confirmed", actor["id"], material=f"{material_id}r{rev}")
            self._refresh_basis_issues(case, at_dt)
            self._save()
            return material

    def withdrawal_active(self, case: dict, material_id: str, rev: int, org: str, at: Any) -> dict | None:
        at_dt = parse_dt(at) or self._now()
        for wd in case.get("withdrawals", []):
            if (
                str(wd["material_id"]) == str(material_id)
                and int(wd["rev"]) == int(rev)
                and wd["party"] == org
                and parse_dt(wd["at"]) <= at_dt
            ):
                return wd
        return None

    def disclosure_active(self, case: dict, material: dict, org: str, at: Any) -> tuple[bool, list[str]]:
        at_dt = parse_dt(at) or self._now()
        reasons = []
        is_owner = material.get("owner") == org
        if org not in material.get("allows", []) and not is_owner:
            reasons.append("not_disclosed_to_party")
        if material.get("kind") in {"redacted", "cover", "excerpt", "extract"} and not material.get("confirmed"):
            reasons.append("redaction_or_excerpt_not_confirmed")
        wd = self.withdrawal_active(case, material["id"], material["rev"], org, at_dt)
        if wd:
            reasons.append("consent_withdrawn")
        return not reasons, reasons

    def withdraw_consent(self, case_id: str, payload: dict, actor_id: str | None = None) -> dict:
        with self._lock:
            actor = self.actor(actor_id or payload.get("actor"))
            case = self._case(case_id)
            mid = payload.get("material_id") or payload.get("material") or payload.get("id")
            rev = int(payload["rev"])
            party = payload["party"]
            material = self._material_rev(case, mid, rev)
            at_dt = self._now(payload.get("at"))
            if not ("clerk" in actor["roles"] or actor["org"] == material["owner"]):
                raise PermissionDeniedError("只有证据归属方或登记其意见的经办人可撤回披露同意")

            wid = payload.get("id") or self._seq("DW")
            if any(w["id"] == wid for w in case["withdrawals"]):
                raise ConflictError(f"撤回记录已存在: {wid}")
            withdrawal = {
                "id": wid,
                "material_id": str(mid),
                "rev": rev,
                "party": party,
                "owner": material["owner"],
                "at": iso(at_dt),
                "actor": actor["id"],
                "reason": payload.get("reason"),
            }
            case["withdrawals"].append(withdrawal)
            stopped = self._stop_undelivered_items(case, withdrawal, at_dt)
            withdrawal["stopped_items"] = stopped
            consequences = self._apply_basis_consequences(case, at_dt, "withdrawal", withdrawal)
            withdrawal["consequences"] = consequences
            self.audit("disclosure.withdrawn", actor["id"], withdrawal_id=wid)
            self._save()
            return withdrawal

    def _stop_undelivered_items(self, case: dict, withdrawal: dict, at_dt) -> list[dict]:
        stopped: list[dict] = []
        wanted = refs_key([withdrawal["material_id"], withdrawal["rev"]])
        for bundle in case["bundles"].values():
            if bundle.get("org") != withdrawal["party"]:
                continue
            for item in bundle.get("items", []):
                if refs_key(item["ref"]) != wanted:
                    continue
                delivered = item.get("carrier_state") == "delivered" and parse_dt(
                    item.get("delivered_at")
                )
                if delivered and parse_dt(item["delivered_at"]) <= at_dt:
                    item["post_withdrawal_status"] = "already_physically_delivered"
                    continue
                if item.get("status") in {"queued", "prepared"}:
                    item["status"] = "stopped_before_delivery"
                    item["stopped_at"] = iso(at_dt)
                    stopped.append({"bundle_id": bundle["bundle_id"], "ref": item["ref"]})
                elif item.get("carrier_state") == "rejected":
                    item["status"] = "delivery_rejected_no_delivery"
                    item["stopped_at"] = iso(at_dt)
                    stopped.append({"bundle_id": bundle["bundle_id"], "ref": item["ref"]})
                elif item.get("carrier_state") == "unknown" or bundle.get("sent_at"):
                    item["stop_requested_at"] = iso(at_dt)
                    item["post_withdrawal_status"] = "carrier_status_unknown_cannot_assume_undelivered"
        return stopped

    def effective_service_exists(
        self, case: dict, material: dict, org: str, at: Any = None
    ) -> dict | None:
        at_dt = parse_dt(at) if at is not None else None
        wanted = refs_key([material["id"], material["rev"]])
        for event in case.get("service_events", []):
            if event["org"] != org:
                continue
            if at_dt and parse_dt(event["at"]) > at_dt:
                continue
            if any(refs_key(ref) == wanted for ref in event.get("docs", [])):
                return event
        return None

    def usable_ref(
        self, case: dict, material: dict, org: str, at: Any
    ) -> tuple[bool, list[str], dict | None]:
        at_dt = parse_dt(at) or self._now()
        active, reasons = self.disclosure_active(case, material, org, at_dt)
        served = self.effective_service_exists(case, material, org, at_dt)
        if not served and material.get("owner") != org:
            reasons.append("no_effective_service")
        elif not served and material.get("owner") == org:
            # 归属方持有原件，不要求向自己送达。
            pass
        usable = not reasons
        return usable, reasons, served

    def usable_materials(
        self, case_id: str, matter_id: str, org: str, at: Any = None
    ) -> list[dict]:
        case = self._case(case_id)
        matter = case["matters"].get(matter_id)
        if not matter:
            raise NotFoundError(f"没有事项 {matter_id}")
        at_dt = self._now(at)
        out = []
        for group in case["materials"].values():
            for material in group["revisions"]:
                if matter["fact"] not in material.get("facts", []):
                    continue
                usable, reasons, served = self.usable_ref(case, material, org, at_dt)
                if usable:
                    out.append(
                        {
                            "ref": [material["id"], material["rev"]],
                            "kind": material["kind"],
                            "owner": material["owner"],
                            "sha256": material["sha256"],
                            "served_event_id": served["id"] if served else None,
                            "served_at": served["at"] if served else None,
                        }
                    )
        return out

    def _refresh_basis_issues(self, case: dict, at_dt) -> None:
        """根据当前披露/送达状态刷新“某方某事实无可答辩材料”的问题。"""
        case.setdefault("remands", [])
        for mid, matter in case.get("matters", {}).items():
            for org in matter.get("affected_parties", []):
                usable = self.usable_materials(case["id"], mid, org, at_dt) if mid in case["matters"] else []
                key = f"{mid}|{org}"
                issue = next((x for x in case["basis_issues"] if x["key"] == key), None)
                remand = next((x for x in case["remands"] if x["key"] == key), None)
                if not usable:
                    if not issue:
                        issue = {"key": key, "matter_id": mid, "party": org, "history": []}
                        case["basis_issues"].append(issue)
                    issue.pop("resolved_at", None)
                    issue.update(
                        {
                            "status": "needs_substitute",
                            "since": iso(at_dt),
                            "excluded_from_party_basis": True,
                            "reason": "没有可向该方合法披露、有效送达且足以答辩的材料",
                        }
                    )
                    if not remand:
                        remand = {
                            "id": "RMD:" + key.replace(":", "-").replace("|", "-"),
                            "key": key,
                            "case_id": case["id"],
                            "matter_id": mid,
                            "party": org,
                        }
                        case["remands"].append(remand)
                    remand.pop("resolved_at", None)
                    remand.update(
                        {
                            "status": "returned_for_substitute",
                            "since": iso(at_dt),
                            "instruction": "从该方裁决依据排除；在有可答辩的替代依据并完成相应期限前，不得作不利维持。",
                        }
                    )
                else:
                    if issue and issue.get("status") == "needs_substitute":
                        issue.update(
                            {
                                "status": "substitute_available",
                                "resolved_at": iso(at_dt),
                                "excluded_from_party_basis": False,
                            }
                        )
                    if remand and remand.get("status") == "returned_for_substitute":
                        remand.update(
                            {
                                "status": "substitute_available",
                                "resolved_at": iso(at_dt),
                                "note": "已有可合法披露、有效送达且足以答辩的替代依据；仍须满足新答辩期限。",
                            }
                        )

    def _apply_basis_consequences(self, case: dict, at_dt, cause: str, source: dict) -> list[dict]:
        self._refresh_basis_issues(case, at_dt)
        consequences = []
        party = source.get("party")
        affected_keys = {
            x["key"]
            for x in case["basis_issues"]
            if x.get("status") == "needs_substitute" and (not party or x.get("party") == party)
        }
        for decision in case.get("decisions", []):
            for item in decision.get("items", []):
                key = f"{item['matter_id']}|{item['party']}"
                if key not in affected_keys:
                    continue
                if item.get("correction_state"):
                    continue
                if self._item_has_physical_receipt(case, item):
                    remedy = self._create_remedy(
                        case,
                        at_dt,
                        cause=cause,
                        source=source,
                        decision=decision,
                        item=item,
                        executed=True,
                    )
                    consequences.append({"type": "executed_referred_to_remedy", "remedy_id": remedy["id"]})
                else:
                    item["correction_state"] = "basis_lost_needs_reconsideration"
                    item["execution_status"] = "blocked_pending_reconsideration"
                    item["basis_lost_at"] = iso(at_dt)
                    self._block_pending_executions(case, item, at_dt)
                    consequences.append(
                        {"type": "unexecuted_item_returned", "decision_id": decision["id"], "item_id": item["id"]}
                    )
        return consequences

    def _public_material(self, case: dict, material: dict, viewer_org: str | None, *, staff: bool, at) -> dict:
        if staff:
            active, reasons = True, []
        else:
            active, reasons = self.disclosure_active(case, material, viewer_org or "", at)
        return {
            "id": material["id"],
            "rev": material["rev"],
            "ref": [material["id"], material["rev"]],
            "owner": material["owner"],
            "kind": material["kind"],
            "facts": material["facts"],
            "allows": material.get("allows", []),
            "confirmed": material.get("confirmed", False),
            "sha256": material["sha256"],
            "created_at": material.get("created_at"),
            "defense_change": material.get("defense_change", False),
            "openable": staff or active,
            "access_reasons": [] if staff else reasons,
            "content": material.get("content") if staff or active else None,
        }

    # ---------- 文件交接、送达回执与期限 ----------

    def create_delivery(self, case_id: str, payload: dict, actor_id: str | None = None) -> dict:
        with self._lock:
            actor = self.require_roles(actor_id or payload.get("actor"), "clerk")
            case = self._case(case_id)
            org = payload.get("org") or payload.get("party")
            person = payload.get("person") or payload.get("actor")
            if not org or not person:
                raise ValidationError("送达需要 org/party 与 person")
            person_record = self.actor(person)
            if person_record["org"] != org:
                raise ValidationError("接收人员与受送达组织不一致")
            docs = [as_ref(x) for x in payload.get("docs", [])]
            if not docs:
                raise ValidationError("送达文件组 docs 不能为空")
            at_dt = self._now(payload.get("created_at") or payload.get("at"))
            for ref in docs:
                material = self._find_ref(case, ref)
                ok, reasons = self.disclosure_active(case, material, org, at_dt)
                if not ok:
                    raise ConflictError(
                        f"文件 {refs_key(ref)} 当前不能披露给 {org}", details={"reasons": reasons}
                    )

            bid = payload.get("bundle_id") or payload.get("bundle") or self._seq("DG")
            if bid in case["bundles"]:
                raise ConflictError(f"送达批次已存在: {bid}")
            bundle = {
                "bundle_id": bid,
                "case_id": case_id,
                "org": org,
                "person": person,
                "doc_group": payload.get("doc_group") or bid,
                "docs": docs,
                "matter_ids": payload.get("matter_ids") or [],
                "changed_matters": payload.get("changed_matters") or [],
                "kind": payload.get("kind", "initial"),
                "note": payload.get("note"),
                "status": "prepared",
                "created_at": iso(at_dt),
                "sent_at": None,
                "items": [{"ref": ref, "status": "queued", "receipt_ids": []} for ref in docs],
                "receipt_ids": [],
                "timeout_checks": [],
                "created_by": actor["id"],
            }
            case["bundles"][bid] = bundle
            send_at = payload.get("sent_at") or (payload.get("send_at") if payload.get("send") else None)
            if payload.get("send") or send_at:
                self.send_delivery(case_id, bid, {"at": send_at or at_dt}, actor["id"])
            self._save()
            return self.delivery_view(case_id, bid)

    def send_delivery(self, case_id: str, bundle_id: str, payload: dict | None = None, actor_id: str | None = None) -> dict:
        with self._lock:
            self.require_roles(actor_id or (payload or {}).get("actor"), "clerk", "delivery")
            case = self._case(case_id)
            bundle = case["bundles"].get(bundle_id)
            if not bundle:
                raise NotFoundError(f"没有送达批次 {bundle_id}")
            if bundle.get("status") == "stopped":
                raise ConflictError("送达批次已停止")
            at_dt = self._now((payload or {}).get("at"))
            for item in bundle["items"]:
                material = self._find_ref(case, item["ref"])
                ok, _ = self.disclosure_active(case, material, bundle["org"], at_dt)
                if not ok and item["status"] in {"queued", "prepared"}:
                    item["status"] = "stopped_before_delivery"
                    item["stopped_at"] = iso(at_dt)
                elif item["status"] in {"queued", "prepared"}:
                    item["status"] = "sent"
            if all(i["status"].startswith("stopped") for i in bundle["items"]):
                bundle["status"] = "stopped"
            else:
                bundle["status"] = "sent"
                bundle["sent_at"] = iso(at_dt)
            self.audit("delivery.sent", actor_id, bundle_id=bundle_id)
            self._save()
            return self.delivery_view(case_id, bundle_id)

    def ingest_receipt(self, case_id: str, receipt: dict, actor_id: str | None = None) -> dict:
        with self._lock:
            if actor_id:
                self.require_roles(actor_id, "clerk", "delivery")
            case = self._case(case_id)
            rid = receipt.get("id")
            bid = receipt.get("bundle") or receipt.get("bundle_id")
            state = receipt.get("state")
            person = receipt.get("person")
            if not rid or not bid or state not in {"unknown", "delivered", "rejected"} or not person:
                raise ValidationError("回执必须包含 id、bundle、person 和合法 state")
            at_dt = self._now(receipt.get("at"))
            docs = [as_ref(x) for x in receipt.get("docs", [])]
            if not docs:
                raise ValidationError("回执 docs 不能为空")
            for ref in docs:
                self._find_ref(case, ref)

            if rid in case["receipts"]:
                old = case["receipts"][rid]
                if old.get("bundle") != bid or old.get("at") != iso(at_dt):
                    raise ConflictError(f"回执 {rid} 已存在且关键内容不同；更正应另留更正标识")
                return old

            bundle = case["bundles"].get(bid)
            if not bundle:
                person_record = self.state["actors"].get(person)
                org = receipt.get("org") or (person_record["org"] if person_record else None)
                if not org:
                    raise ValidationError(f"未知接收人 {person}，且回执未提供 org")
                bundle = {
                    "bundle_id": bid,
                    "case_id": case_id,
                    "org": org,
                    "person": person,
                    "doc_group": receipt.get("doc_group") or bid,
                    "docs": docs,
                    "matter_ids": receipt.get("matter_ids") or [],
                    "changed_matters": receipt.get("changed_matters") or [],
                    "kind": receipt.get("kind", "initial"),
                    "status": "sent",
                    "created_at": iso(at_dt),
                    "sent_at": receipt.get("sent_at"),
                    "items": [{"ref": ref, "status": "sent", "receipt_ids": []} for ref in docs],
                    "receipt_ids": [],
                    "timeout_checks": [],
                    "auto_created_from_receipt": True,
                }
                case["bundles"][bid] = bundle

            org = bundle["org"]
            record = {
                "id": rid,
                "bundle": bid,
                "person": person,
                "org": org,
                "state": state,
                "at": iso(at_dt),
                "sent_at": receipt.get("sent_at"),
                "docs": docs,
                "raw": receipt,
            }

            effective_docs: list[list] = []
            reasons: list[str] = []
            mandate = None
            if state != "delivered":
                reasons.append("carrier_status_" + state)
            else:
                mandate = self.active_mandate(case, org, person, at_dt, doc_group=bundle.get("doc_group"))
                if not mandate:
                    reasons.append("recipient_not_authorized_at_delivery")
                for ref in docs:
                    material = self._find_ref(case, ref)
                    ok, doc_reasons = self.disclosure_active(case, material, org, at_dt)
                    if ok and mandate:
                        effective_docs.append(ref)
                    else:
                        reasons.extend([f"{refs_key(ref)}:{r}" for r in doc_reasons])
                if not effective_docs and not reasons:
                    reasons.append("no_effective_docs")

            record["evaluation"] = {
                "physical_delivered": state == "delivered",
                "effective": bool(effective_docs),
                "effective_docs": effective_docs,
                "reasons": sorted(set(reasons)),
                "mandate": mandate,
                "recognized_original_bundle": bid,
            }
            case["receipts"][rid] = record
            if rid not in case["receipt_order"]:
                case["receipt_order"].append(rid)
            if rid not in bundle["receipt_ids"]:
                bundle["receipt_ids"].append(rid)

            for ref in docs:
                item = next((i for i in bundle["items"] if refs_key(i["ref"]) == refs_key(ref)), None)
                if not item:
                    item = {"ref": ref, "status": "sent", "receipt_ids": []}
                    bundle["items"].append(item)
                if rid not in item["receipt_ids"]:
                    item["receipt_ids"].append(rid)
                item["last_receipt_id"] = rid
                item["carrier_state"] = state
                item["state_at"] = iso(at_dt)
                if state == "delivered":
                    item["delivered_at"] = iso(at_dt)
                    item["status"] = "delivered"
                elif state == "rejected":
                    item["rejected_at"] = iso(at_dt)
                    item["status"] = "rejected"
                else:
                    item["status"] = "carrier_unknown"

            if effective_docs:
                event_id = f"SE-{rid}"
                if not any(e["id"] == event_id for e in case["service_events"]):
                    event = {
                        "id": event_id,
                        "bundle_id": bid,
                        "receipt_id": rid,
                        "org": org,
                        "person": person,
                        "at": iso(at_dt),
                        "docs": effective_docs,
                        "mandate_id": mandate["id"] if mandate else None,
                    }
                    case["service_events"].append(event)
                    self._open_deadlines(case, bundle, event, at_dt)

            self._refresh_bundle_status(case, bundle)
            self._refresh_basis_issues(case, at_dt)
            self.audit("receipt.ingested", actor_id or "delivery-service", receipt_id=rid, bundle=bid)
            self._save()
            return record

    def _refresh_bundle_status(self, case: dict, bundle: dict) -> None:
        receipts = [case["receipts"][rid] for rid in bundle["receipt_ids"] if rid in case["receipts"]]
        if any(r["state"] == "delivered" for r in receipts):
            bundle["carrier_state"] = "delivered"
        elif any(r["state"] == "rejected" for r in receipts):
            bundle["carrier_state"] = "rejected"
        else:
            bundle["carrier_state"] = "unknown"
        effective = any(r.get("evaluation", {}).get("effective") for r in receipts)
        bundle["effective"] = effective
        if any(i.get("status", "").startswith("stopped") for i in bundle["items"]) and not effective:
            bundle["status"] = "stopped_or_blocked"
        elif effective:
            bundle["status"] = "effective_service"
        else:
            bundle["status"] = bundle["carrier_state"]

    def record_timeout_check(self, case_id: str, bundle_id: str, payload: dict | None = None, actor_id: str | None = None) -> dict:
        with self._lock:
            self.require_roles(actor_id or (payload or {}).get("actor"), "clerk", "delivery")
            case = self._case(case_id)
            bundle = case["bundles"].get(bundle_id)
            if not bundle:
                raise NotFoundError(f"没有送达批次 {bundle_id}")
            at_dt = self._now((payload or {}).get("at"))
            check = {"at": iso(at_dt), "note": (payload or {}).get("note"), "result": "unknown_not_definite_nondelivery"}
            bundle.setdefault("timeout_checks", []).append(check)
            self.audit("delivery.timeout_checked", actor_id, bundle_id=bundle_id)
            self._save()
            view = self.delivery_view(case_id, bundle_id)
            view["definite_nondelivery"] = False
            return view

    def _matter_ids_for_event(self, case: dict, org: str, event: dict, explicit: list[str]) -> list[str]:
        if explicit:
            return [m for m in explicit if m in case["matters"] and org in case["matters"][m]["affected_parties"]]
        ids = []
        for mid, matter in case["matters"].items():
            if org not in matter["affected_parties"]:
                continue
            for ref in event["docs"]:
                material = self._find_ref(case, ref)
                if matter["fact"] in material.get("facts", []):
                    ids.append(mid)
                    break
        return list(dict.fromkeys(ids))

    def _open_deadlines(self, case: dict, bundle: dict, event: dict, at_dt) -> None:
        explicit = bundle.get("matter_ids") or []
        matter_ids = self._matter_ids_for_event(case, event["org"], event, explicit)
        hours = float(case.get("files_due_hours", 12))
        for mid in matter_ids:
            changed = bool(bundle.get("defense_change")) or mid in (bundle.get("changed_matters") or [])
            existing = [d for d in case["deadlines"] if d["matter_id"] == mid and d["org"] == event["org"]]
            already_for_receipt = any(d.get("receipt_id") == event["receipt_id"] and d["matter_id"] == mid for d in existing)
            if already_for_receipt:
                continue
            if existing and not changed:
                # 补件中无关的既有事项不重新起算；旧期限继续。
                continue
            generation = (max(int(d["generation"]) for d in existing) + 1) if existing else 1
            due = at_dt + timedelta(hours=hours)
            safe_mid = mid.replace(":", "-").replace("/", "-")
            deadline = {
                "id": f"DL-{event['receipt_id']}-{safe_mid}-G{generation}",
                "case_id": case["id"],
                "org": event["org"],
                "matter_id": mid,
                "generation": generation,
                "bundle_id": bundle["bundle_id"],
                "receipt_id": event["receipt_id"],
                "served_at": iso(at_dt),
                "due_at": iso(due),
                "full_period": True,
                "resets_previous": changed,
                "doc_refs": event["docs"],
                "status": "controlling",
            }
            case["deadlines"].append(deadline)

    def current_deadline(self, case: dict, matter_id: str, org: str) -> dict | None:
        items = [d for d in case["deadlines"] if d["matter_id"] == matter_id and d["org"] == org]
        if not items:
            return None
        return max(items, key=lambda d: (int(d["generation"]), parse_dt(d["served_at"])))

    def deadline_state(self, case: dict, deadline: dict | None, at: Any) -> dict:
        at_dt = parse_dt(at) or self._now()
        if not deadline:
            return {"state": "unserved", "deadline_id": None}
        answered = any(
            a["org"] == deadline["org"]
            and any(
                i.get("matter_id") == deadline["matter_id"]
                and int(i.get("generation", 0)) == int(deadline["generation"])
                for i in a.get("items", [])
            )
            for a in case.get("answers", [])
        )
        waived = any(
            w["org"] == deadline["org"]
            and (
                w.get("deadline_id") == deadline["id"]
                or any(
                    x.get("matter_id") == deadline["matter_id"]
                    and int(x.get("generation", 0)) == int(deadline["generation"])
                    for x in w.get("items", [])
                )
            )
            for w in case.get("waivers", [])
        )
        due = parse_dt(deadline["due_at"])
        if answered:
            state = "answered"
        elif waived:
            state = "waived"
        elif at_dt >= due:
            state = "expired"
        else:
            state = "open"
        return {
            "state": state,
            "deadline_id": deadline["id"],
            "generation": deadline["generation"],
            "served_at": deadline["served_at"],
            "due_at": deadline["due_at"],
            "remaining_seconds": max(0, int((due - at_dt).total_seconds())) if state == "open" else 0,
        }

    def file_answer(self, case_id: str, payload: dict, actor_id: str | None = None) -> dict:
        with self._lock:
            actor = self.actor(actor_id or payload.get("actor"))
            case = self._case(case_id)
            at_dt = self._now(payload.get("at"))
            target_actor = actor
            if "clerk" in actor.get("roles", []) and payload.get("by"):
                target_actor = self.actor(payload["by"])
            self.require_mandate(case, target_actor, at_dt)
            org = target_actor["org"]
            raw_items = payload.get("items")
            if raw_items is None and payload.get("targets"):
                raw_items = [{"matter_id": self._matter_id_for_target(case, payload), "text": payload.get("text", "")}]
            if not raw_items:
                raise ValidationError("答辩至少包含一个 items")

            items = []
            for raw in raw_items:
                mid = raw.get("matter_id")
                matter = case["matters"].get(mid)
                if not matter:
                    raise NotFoundError(f"没有事项 {mid}")
                if org not in matter["affected_parties"]:
                    raise PermissionDeniedError("只能答辩影响本组织的事项")
                deadline = self.current_deadline(case, mid, org)
                procedure = self.deadline_state(case, deadline, at_dt)
                if procedure["state"] == "unserved":
                    raise ConflictError("该事项尚无有效送达，不能提交正式答辩", details=procedure)
                item = {
                    "matter_id": mid,
                    "generation": deadline["generation"],
                    "deadline_id": deadline["id"],
                    "text": raw.get("text", ""),
                    "requests_new_fact_finding": raw.get("requests_new_fact_finding", True),
                    "evidence_refs": [as_ref(x) for x in raw.get("evidence_refs", [])],
                    "timely": at_dt <= parse_dt(deadline["due_at"]),
                    "procedure_at_submission": procedure["state"],
                    "material_refs_snapshot": list(deadline.get("doc_refs", [])),
                }
                items.append(item)

            answer = {
                "id": payload.get("id") or self._seq("ANS"),
                "case_id": case_id,
                "org": org,
                "actor": target_actor["id"],
                "recorded_by": actor["id"],
                "at": iso(at_dt),
                "items": items,
                "attachments": payload.get("attachments", []),
            }
            case["answers"].append(answer)
            self.audit("answer.filed", actor["id"], answer_id=answer["id"])
            self._save()
            return answer

    def _matter_id_for_target(self, case: dict, payload: dict) -> str:
        sid = payload.get("subject") or payload.get("subject_id")
        fact = payload.get("fact") or (payload.get("targets") or [None])[0]
        mid = f"{sid}:{fact}"
        if mid not in case["matters"]:
            raise ValidationError(f"无法定位事项 {mid}")
        return mid

    def waive_period(self, case_id: str, payload: dict, actor_id: str | None = None) -> dict:
        with self._lock:
            actor = self.actor(actor_id or payload.get("actor"))
            case = self._case(case_id)
            at_dt = self._now(payload.get("at"))
            target = actor
            if "clerk" in actor.get("roles", []) and payload.get("by"):
                target = self.actor(payload["by"])
            self.require_mandate(case, target, at_dt)
            org = target["org"]
            items = []
            if payload.get("deadline_id"):
                dl = next((d for d in case["deadlines"] if d["id"] == payload["deadline_id"]), None)
                if not dl or dl["org"] != org:
                    raise NotFoundError("没有属于该方的期限")
                items.append({"matter_id": dl["matter_id"], "generation": dl["generation"]})
            for raw in payload.get("items", []):
                dl = self.current_deadline(case, raw["matter_id"], org)
                if not dl:
                    raise ConflictError("事项尚无期限")
                items.append({"matter_id": raw["matter_id"], "generation": dl["generation"]})
            if not items:
                raise ValidationError("放弃余期须指定 deadline_id 或 items")
            waiver = {
                "id": payload.get("id") or self._seq("WV"),
                "case_id": case_id,
                "org": org,
                "actor": target["id"],
                "at": iso(at_dt),
                "deadline_id": payload.get("deadline_id"),
                "items": items,
                "scope": "remaining_period_only",
            }
            case["waivers"].append(waiver)
            self.audit("period.waived", actor["id"], waiver_id=waiver["id"])
            self._save()
            return waiver

    def _deadline_summaries(self, case: dict, at_dt) -> list[dict]:
        result = []
        seen = set()
        for deadline in case.get("deadlines", []):
            key = (deadline["matter_id"], deadline["org"])
            current = self.current_deadline(case, deadline["matter_id"], deadline["org"])
            if key in seen or not current or deadline["id"] != current["id"]:
                continue
            seen.add(key)
            state = self.deadline_state(case, current, at_dt)
            issue = next(
                (
                    i
                    for i in case["basis_issues"]
                    if i["key"] == f"{current['matter_id']}|{current['org']}"
                    and i.get("status") == "needs_substitute"
                ),
                None,
            )
            state["basis_issue"] = issue
            result.append(state)
        return result

    # ---------- 复核决定、执行与实物回执 ----------

    def make_decision(self, case_id: str, payload: dict, actor_id: str | None = None) -> dict:
        with self._lock:
            actor = self.require_roles(actor_id or payload.get("actor"), "reviewer")
            case = self._case(case_id)
            at_dt = self._now(payload.get("at"))
            raw_items = payload.get("items")
            if isinstance(raw_items, dict):
                raw_items = [raw_items]
            if not raw_items:
                raise ValidationError("决定至少包含一个 items")

            prepared = []
            for index, raw in enumerate(raw_items, start=1):
                mid = raw.get("matter_id")
                if not mid:
                    sid = raw.get("subject_id") or raw.get("subject")
                    mid = f"{sid}:{raw['fact']}"
                matter = case["matters"].get(mid)
                if not matter:
                    raise NotFoundError(f"没有事项 {mid}")
                party = raw.get("party") or matter["party"]
                if party not in matter["affected_parties"]:
                    raise ValidationError(f"{party} 不是事项 {mid} 的受影响方")
                outcome = raw.get("outcome")
                if outcome not in DECISION_OUTCOMES:
                    raise ValidationError(f"非法决定结果: {outcome}")

                deadline = self.current_deadline(case, mid, party)
                procedure = self.deadline_state(case, deadline, at_dt)
                if procedure["state"] not in FINAL_PROCEDURE_STATES:
                    raise ConflictError(
                        "答辩期尚未期满、无合法答辩且未明确放弃余期，不能作出该决定",
                        details={"matter_id": mid, "party": party, "procedure": procedure},
                    )

                cited = [as_ref(x) for x in raw.get("cited_materials", [])]
                snapshots = []
                if outcome == "uphold_reject":
                    if not cited:
                        # 若复核人未显式列举，则采用当前全部可用材料，但决定仍记录具体版本。
                        cited = [x["ref"] for x in self.usable_materials(case_id, mid, party, at_dt)]
                    if not cited:
                        raise ConflictError(
                            "该事实对受影响方没有可答辩的依据，应恢复收货或要求补查",
                            details={"matter_id": mid, "party": party},
                        )
                    for ref in cited:
                        material = self._find_ref(case, ref)
                        if matter["fact"] not in material.get("facts", []):
                            raise ValidationError(
                                f"{refs_key(ref)} 不能证明决定事实 {matter['fact']}"
                            )
                        usable, reasons, served = self.usable_ref(case, material, party, at_dt)
                        if not usable:
                            raise ConflictError(
                                f"裁决引用材料 {refs_key(ref)} 对 {party} 不可作为依据",
                                details={"reasons": reasons},
                            )
                        snapshots.append(
                            {
                                "ref": ref,
                                "sha256": material["sha256"],
                                "owner": material["owner"],
                                "kind": material["kind"],
                                "facts": material["facts"],
                                "service_event_id": served["id"] if served else None,
                                "served_at": served["at"] if served else None,
                                "bundle_id": served["bundle_id"] if served else None,
                                "receipt_id": served["receipt_id"] if served else None,
                            }
                        )

                independent = bool(raw.get("independently_executable", False))
                if outcome == "uphold_reject" and raw.get("independently_executable") is None:
                    independent = bool(matter.get("independently_executable"))
                item = {
                    "client_item_id": raw.get("item_id") or raw.get("client_item_id"),
                    "index": index,
                    "matter_id": mid,
                    "subject_id": matter["subject_id"],
                    "fact": matter["fact"],
                    "party": party,
                    "outcome": outcome,
                    "reason": raw.get("reason", ""),
                    "cited_materials": cited,
                    "basis_snapshot": snapshots,
                    "procedure_snapshot": {
                        "decided_at": iso(at_dt),
                        **procedure,
                    },
                    "independently_executable": independent,
                    "execution_spec": raw.get("execution")
                    or ({"action": "transfer_or_dispose"} if outcome == "uphold_reject" else None),
                    "status": "effective",
                    "correction_state": None,
                    "execution_status": self._initial_execution_status(outcome, independent),
                    "decided_at": iso(at_dt),
                }
                prepared.append(item)

            did = payload.get("decision_id") or payload.get("id") or self._seq("DEC")
            if any(d["id"] == did for d in case["decisions"]):
                raise ConflictError(f"决定已存在: {did}")
            decision = {
                "id": did,
                "case_id": case_id,
                "reviewer": actor["id"],
                "at": iso(at_dt),
                "summary": payload.get("summary", ""),
                "items": [],
            }
            for item in prepared:
                item["id"] = f"{did}:I{item['index']}"
                decision["items"].append(item)
                matter = case["matters"][item["matter_id"]]
                matter["status"] = {
                    "uphold_reject": "upheld",
                    "restore_receipt": "restored",
                    "supplement_investigation": "supplementing",
                }[item["outcome"]]
                if item["outcome"] == "supplement_investigation":
                    inv = {
                        "id": self._seq("INV"),
                        "case_id": case_id,
                        "matter_id": item["matter_id"],
                        "party": item["party"],
                        "decision_id": did,
                        "status": "open",
                        "instruction": item["reason"] or "补充可向受影响方披露并使其足以答辩的依据",
                        "at": iso(at_dt),
                    }
                    case["investigations"].append(inv)
                    item["investigation_id"] = inv["id"]

            case["decisions"].append(decision)
            case["status"] = "decided_or_partial"
            self.audit("decision.made", actor["id"], decision_id=did)
            self._save()
            return decision

    @staticmethod
    def _initial_execution_status(outcome: str, independent: bool) -> str:
        if outcome == "uphold_reject":
            return "pending_execution" if independent else "held_with_related_matters"
        if outcome == "restore_receipt":
            return "pending_restore" if independent else "held_with_related_matters"
        return "not_executable_investigation"

    def _find_decision_item(self, case: dict, decision_id: str, item_id: str) -> tuple[dict, dict]:
        decision = next((d for d in case["decisions"] if d["id"] == decision_id), None)
        if not decision:
            raise NotFoundError(f"没有决定 {decision_id}")
        item = next(
            (i for i in decision["items"] if i["id"] == item_id or i.get("client_item_id") == item_id),
            None,
        )
        if not item:
            raise NotFoundError(f"决定 {decision_id} 没有事项 {item_id}")
        return decision, item

    def create_execution(self, case_id: str, payload: dict, actor_id: str | None = None) -> dict:
        with self._lock:
            actor = self.require_roles(actor_id or payload.get("actor"), "warehouse", "factory")
            case = self._case(case_id)
            decision_id = payload["decision_id"]
            item_id = payload["item_id"]
            decision, item = self._find_decision_item(case, decision_id, item_id)
            if item.get("correction_state") == "needs_reconsideration" or item.get("correction_state") == "basis_lost_needs_reconsideration":
                raise ConflictError("该部分送达/依据范围已纠正，尚未重新判断，不能执行")
            if item.get("correction_state") == "remedy_referred":
                raise ConflictError("该部分已执行并转补救，不能重新列为待转处")
            if not item.get("independently_executable"):
                raise ConflictError("该决定部分不是可独立执行部分")
            if item["outcome"] not in {"uphold_reject", "restore_receipt"}:
                raise ConflictError("补查决定不能进入实物转处")
            if item["execution_status"] == "executed":
                raise ConflictError("该事项已有实物办结；不能重新列成待转处，后续应走补救")
            if item["execution_status"] == "blocked_pending_reconsideration":
                raise ConflictError("该事项已被阻止执行，等待重新判断")

            action = payload.get("action")
            if not action:
                action = "restore_receive" if item["outcome"] == "restore_receipt" else "transfer_or_dispose"
            execution = {
                "id": payload.get("id") or self._seq("EXEC"),
                "case_id": case_id,
                "decision_id": decision_id,
                "item_id": item["id"],
                "batch": case["subjects"][item["subject_id"]]["batch"],
                "supplier": item["party"],
                "action": action,
                "quantity": payload.get("quantity"),
                "unit": payload.get("unit"),
                "carrier": payload.get("carrier"),
                "destination": payload.get("destination"),
                "note": payload.get("note"),
                "requested_by": actor["id"],
                "created_at": iso(self._now(payload.get("at"))),
                "status": "ordered",
                "receipts": [],
            }
            case["executions"].append(execution)
            item["execution_status"] = "execution_ordered"
            self.audit("execution.created", actor["id"], execution_id=execution["id"])
            self._save()
            return execution

    def record_physical_receipt(
        self, case_id: str, execution_id: str, payload: dict, actor_id: str | None = None
    ) -> dict:
        with self._lock:
            actor = self.require_roles(actor_id or payload.get("actor"), "warehouse")
            case = self._case(case_id)
            execution = next((e for e in case["executions"] if e["id"] == execution_id), None)
            if not execution:
                raise NotFoundError(f"没有执行单 {execution_id}")
            at_dt = self._now(payload.get("at"))
            receipt = {
                "id": payload.get("id") or self._seq("PR"),
                "execution_id": execution_id,
                "case_id": case_id,
                "kind": payload.get("kind") or payload.get("receipt_type") or "physical_receipt",
                "at": iso(at_dt),
                "quantity": payload.get("quantity", execution.get("quantity")),
                "unit": payload.get("unit", execution.get("unit")),
                "received_by": payload.get("received_by") or actor["id"],
                "location": payload.get("location"),
                "note": payload.get("note"),
                "photo_refs": payload.get("photo_refs", []),
            }
            case["physical_receipts"].append(receipt)
            execution["receipts"].append(
                {"physical_receipt_id": receipt["id"], "at": receipt["at"], "quantity": receipt["quantity"]}
            )
            execution["status"] = "receipted"
            execution["physical_receipt_id"] = receipt["id"]

            _, item = self._find_decision_item(case, execution["decision_id"], execution["item_id"])
            item["execution_status"] = "executed"
            item["executed_at"] = receipt["at"]
            item["physical_receipt_ids"] = list(dict.fromkeys(
                item.get("physical_receipt_ids", []) + [receipt["id"]]
            ))
            self.audit("physical_receipt.recorded", actor["id"], receipt_id=receipt["id"])
            self._save()
            return receipt

    def _item_has_physical_receipt(self, case: dict, item: dict) -> bool:
        return bool(item.get("physical_receipt_ids")) or any(
            e["item_id"] == item["id"] and e.get("receipts") for e in case.get("executions", [])
        )

    def _block_pending_executions(self, case: dict, item: dict, at_dt) -> None:
        for execution in case.get("executions", []):
            if execution["item_id"] != item["id"]:
                continue
            if not execution.get("receipts"):
                execution["status"] = "blocked_pending_reconsideration"
                execution["blocked_at"] = iso(at_dt)

    def _create_remedy(
        self, case: dict, at_dt, *, cause: str, source: dict, decision: dict, item: dict, executed: bool
    ) -> dict:
        existing = item.get("remedy_id")
        if existing:
            return next(r for r in case["remedies"] if r["id"] == existing)
        executions = [e for e in case["executions"] if e["item_id"] == item["id"]]
        receipt_ids = [rid for e in executions for rid in e.get("receipts", [])]
        remedy = {
            "id": self._seq("REM"),
            "case_id": case["id"],
            "created_at": iso(at_dt),
            "cause": cause,
            "source": {"type": source.get("id") and cause, "record": source},
            "decision_id": decision["id"],
            "item_id": item["id"],
            "assigned_role": "remedy_authority",
            "status": "pending_authorized_handler",
            "executed_part_preserved": executed,
            "facts": {
                "matter_id": item["matter_id"],
                "subject_id": item["subject_id"],
                "fact": item["fact"],
                "party": item["party"],
                "outcome": item["outcome"],
                "cited_materials": item.get("cited_materials", []),
                "basis_snapshot": item.get("basis_snapshot", []),
                "procedure_snapshot": item.get("procedure_snapshot", {}),
            },
            "execution_orders": executions,
            "physical_receipt_refs": receipt_ids,
            "instruction": "已执行部分不得重新列为待转处；由有权经办人评估回转、赔偿、补正或其他补救。",
        }
        case["remedies"].append(remedy)
        item["correction_state"] = "remedy_referred"
        item["remedy_id"] = remedy["id"]
        return remedy

    def correct_service_scope(self, case_id: str, payload: dict, actor_id: str | None = None) -> dict:
        with self._lock:
            actor = self.require_roles(actor_id or payload.get("actor"), "clerk", "reviewer")
            case = self._case(case_id)
            at_dt = self._now(payload.get("at"))
            cid = payload.get("correction_id") or self._seq("COR")
            if any(c["id"] == cid for c in case["corrections"]):
                raise ConflictError(f"纠正记录已存在: {cid}")

            targets = payload.get("targets") or []
            if payload.get("decision_id") and not targets:
                decision = next(d for d in case["decisions"] if d["id"] == payload["decision_id"])
                item_ids = payload.get("item_ids") or [i["id"] for i in decision["items"]]
                targets = [{"decision_id": payload["decision_id"], "item_id": x} for x in item_ids]
            if not targets:
                raise ValidationError("纠正须指定 decision_id/item_ids 或 targets")

            correction = {
                "id": cid,
                "case_id": case_id,
                "at": iso(at_dt),
                "actor": actor["id"],
                "description": payload.get("description", "纠正送达范围"),
                "results": [],
            }
            for target in targets:
                decision, item = self._find_decision_item(case, target["decision_id"], target["item_id"])
                result = {"decision_id": decision["id"], "item_id": item["id"]}
                if self._item_has_physical_receipt(case, item):
                    remedy = self._create_remedy(
                        case,
                        at_dt,
                        cause="service_scope_corrected",
                        source=correction,
                        decision=decision,
                        item=item,
                        executed=True,
                    )
                    result.update(action="executed_part_referred_to_remedy", remedy_id=remedy["id"])
                else:
                    item["correction_state"] = "needs_reconsideration"
                    item["execution_status"] = "blocked_pending_reconsideration"
                    item["service_corrected_at"] = iso(at_dt)
                    item["service_correction_id"] = cid
                    self._block_pending_executions(case, item, at_dt)
                    result.update(action="unexecuted_part_blocked_for_redecision")
                correction["results"].append(result)

            case["corrections"].append(correction)
            self.audit("service_scope.corrected", actor["id"], correction_id=cid)
            self._save()
            return correction

    def list_remands(self, case_id: str, actor_id: str | None = None) -> list[dict]:
        case = self._case(case_id)
        if actor_id:
            actor = self.actor(actor_id)
            if not self._full_evidence_access(actor):
                org = actor["org"]
                return [r for r in case.get("remands", []) if r.get("party") == org]
        return list(case.get("remands", []))

    def list_remedies(self, actor_id: str | None = None) -> list[dict]:
        with self._lock:
            if actor_id:
                actor = self.actor(actor_id)
                if not (self._is_staff(actor) or "remedy_authority" in actor.get("roles", [])):
                    raise PermissionDeniedError("只有有权经办人可核对补救事项")
            return [r for case in self.state["cases"].values() for r in case.get("remedies", [])]

    # ---------- 查询视图 ----------

    def _full_evidence_access(self, actor: dict | None) -> bool:
        return bool(actor and ({"clerk", "reviewer"} & set(actor.get("roles", []))))

    def delivery_view(self, case_id: str, bundle_id: str) -> dict:
        case = self._case(case_id)
        bundle = case["bundles"].get(bundle_id)
        if not bundle:
            raise NotFoundError(f"没有送达批次 {bundle_id}")
        receipts = [case["receipts"][rid] for rid in bundle.get("receipt_ids", []) if rid in case["receipts"]]
        return {
            **bundle,
            "receipts": sorted(receipts, key=lambda r: parse_dt(r["at"])),
            "recognized_original_delivery": bundle_id,
            "timeout_is_not_definite_nondelivery": True,
        }

    def list_deliveries(self, case_id: str, actor_id: str | None = None) -> list[dict]:
        case = self._case(case_id)
        if actor_id:
            actor = self.actor(actor_id)
            if not self._full_evidence_access(actor):
                raise PermissionDeniedError("只有经办/复核人员可核对全部送达")
        return [self.delivery_view(case_id, bid) for bid in sorted(case["bundles"])]

    def party_view(self, case_id: str, org: str, at: Any = None, *, actor: dict | None = None) -> dict:
        case = self._case(case_id)
        at_dt = self._now(at)
        full = self._full_evidence_access(actor)
        operational = bool(actor and ({"factory", "warehouse"} & set(actor.get("roles", []))))
        see_all_matters = full or operational
        materials = []
        for group in case["materials"].values():
            for material in group["revisions"]:
                visible = full or self._material_visible_to_org(case, material, org)
                if not visible:
                    continue
                public = self._public_material(case, material, org, staff=full, at=at_dt)
                physical = self.physical_delivery(case, material, org)
                effective = self.effective_service_exists(case, material, org, at_dt)
                public.update(
                    {
                        "physically_delivered": physical is not None,
                        "physical_delivery": physical,
                        "served_effectively": effective is not None,
                        "effective_service": effective,
                        "served": effective is not None,
                        "openable_now": public["openable"],
                    }
                )
                materials.append(public)

        matters = []
        for mid, matter in case["matters"].items():
            if not see_all_matters and org not in matter["affected_parties"]:
                continue
            if operational and not full:
                party_procedures = {}
                party_basis = {}
                for party in matter["affected_parties"]:
                    deadline = self.current_deadline(case, mid, party)
                    party_procedures[party] = (
                        self.deadline_state(case, deadline, at_dt)
                        if deadline
                        else {"state": "unserved"}
                    )
                    party_basis[party] = bool(self.usable_materials(case_id, mid, party, at_dt))
                matters.append(
                    {
                        **matter,
                        "party_procedures": party_procedures,
                        "party_basis_available": party_basis,
                    }
                )
                continue

            deadline = self.current_deadline(case, mid, org)
            procedure = self.deadline_state(case, deadline, at_dt) if deadline else {"state": "unserved"}
            issue = next(
                (i for i in case["basis_issues"] if i["key"] == f"{mid}|{org}" and i.get("status") == "needs_substitute"),
                None,
            )
            usable = self.usable_materials(case_id, mid, org, at_dt)
            matters.append(
                {
                    **matter,
                    "procedure": procedure,
                    "basis_available": bool(usable),
                    "usable_materials": usable,
                    "basis_issue": issue,
                }
            )

        answers = [a for a in case["answers"] if full or a["org"] == org]
        waivers = [w for w in case["waivers"] if full or w["org"] == org]
        decisions = []
        for decision in case["decisions"]:
            items = [
                i
                for i in decision["items"]
                if full or operational or i["party"] == org
            ]
            if not items:
                continue
            decisions.append(
                {
                    "id": decision["id"],
                    "at": decision["at"],
                    "summary": decision.get("summary"),
                    "reviewer": decision.get("reviewer") if full else None,
                    "items": items if full else [{k: v for k, v in i.items() if k != "basis_snapshot"} for i in items],
                }
            )

        return {
            "case_id": case_id,
            "org": org,
            "viewed_at": iso(at_dt),
            "purchase": case.get("purchase"),
            "status": case.get("status"),
            "appeal_deadline": case.get("appeal_deadline"),
            "matters": matters,
            "materials": materials,
            "deadlines": (
                self._deadline_summaries(case, at_dt)
                if operational and not full
                else [m["procedure"] for m in matters if m.get("procedure")]
            ),
            "answers": answers,
            "waivers": waivers,
            "decisions": decisions,
            "remands": [
                r
                for r in case.get("remands", [])
                if full or operational or r.get("party") == org
            ],
            "holds": [h for h in case.get("holds", []) if full or operational or h.get("supplier") == org],
            "served_index": self.served_and_openable(case_id, org=org, at=at_dt),
        }

    def _material_visible_to_org(self, case: dict, material: dict, org: str) -> bool:
        if material.get("owner") == org or org in material.get("allows", []):
            return True
        wanted = refs_key([material["id"], material["rev"]])
        for bundle in case.get("bundles", {}).values():
            if bundle.get("org") == org and any(refs_key(r) == wanted for r in bundle.get("docs", [])):
                return True
        return False

    def physical_delivery(self, case: dict, material: dict, org: str) -> dict | None:
        wanted = refs_key([material["id"], material["rev"]])
        latest = None
        for rid in case.get("receipt_order", []):
            receipt = case["receipts"][rid]
            if receipt.get("org") != org or receipt.get("state") != "delivered":
                continue
            if not any(refs_key(ref) == wanted for ref in receipt.get("docs", [])):
                continue
            candidate = {
                "receipt_id": rid,
                "bundle_id": receipt["bundle"],
                "at": receipt["at"],
                "person": receipt.get("person"),
                "effective": receipt.get("evaluation", {}).get("effective", False),
                "reasons": receipt.get("evaluation", {}).get("reasons", []),
            }
            if latest is None or parse_dt(candidate["at"]) > parse_dt(latest["at"]):
                latest = candidate
        return latest

    def served_and_openable(
        self, case_id: str, actor_id: str | None = None, *, org: str | None = None, at: Any = None
    ) -> dict:
        case = self._case(case_id)
        at_dt = self._now(at)
        actor = self.actor(actor_id) if actor_id else None
        viewer_org = org or (actor["org"] if actor else None)
        if not viewer_org:
            raise ValidationError("需要 actor 或 org")
        full = self._full_evidence_access(actor)
        served = []
        openable = []
        for group in case["materials"].values():
            for material in group["revisions"]:
                if not full and not self._material_visible_to_org(case, material, viewer_org):
                    continue
                effective = self.effective_service_exists(case, material, viewer_org, at_dt)
                physical = self.physical_delivery(case, material, viewer_org)
                active, reasons = self.disclosure_active(case, material, viewer_org, at_dt)
                ref = [material["id"], material["rev"]]
                if physical or effective:
                    served.append(
                        {
                            "ref": ref,
                            "physical_delivery": physical,
                            "effective_service": effective,
                            "served": bool(effective),
                        }
                    )
                if full or active:
                    openable.append({"ref": ref, "openable": True})
                else:
                    openable.append({"ref": ref, "openable": False, "reasons": reasons})
        return {"served": served, "openable": openable}

    def get_material_content(
        self, case_id: str, material_id: str, rev: int, actor_id: str, at: Any = None
    ) -> dict:
        with self._lock:
            actor = self.actor(actor_id)
            case = self._case(case_id)
            at_dt = self._now(at)
            material = self._material_rev(case, material_id, int(rev))
            if self._full_evidence_access(actor):
                allowed = True
                reasons = []
            else:
                if actor["org"] != material.get("owner"):
                    self.require_mandate(case, actor, at_dt)
                allowed, reasons = self.disclosure_active(case, material, actor["org"], at_dt)
                if not self._material_visible_to_org(case, material, actor["org"]):
                    allowed = False
                    reasons = ["not_visible_to_party"] + reasons
            if not allowed:
                raise PermissionDeniedError(
                    "此版本此刻不能打开；送达记录仍可查询",
                    details={"reasons": reasons, "served_index": self.served_and_openable(case_id, actor_id, at=at_dt)},
                )
            return {
                "ref": [material["id"], material["rev"]],
                "owner": material["owner"],
                "sha256": material["sha256"],
                "content": material["content"],
                "facts": material["facts"],
            }

    def verification_view(self, case_id: str, actor_id: str, at: Any = None) -> dict:
        actor = self.require_roles(actor_id, "clerk", "reviewer")
        case = self._case(case_id)
        at_dt = self._now(at)
        proof_map = {}
        for mid, matter in case["matters"].items():
            party_rows = {}
            for org in matter["affected_parties"]:
                all_for_party = []
                excluded = []
                for group in case["materials"].values():
                    for material in group["revisions"]:
                        if matter["fact"] not in material.get("facts", []):
                            continue
                        usable, reasons, served = self.usable_ref(case, material, org, at_dt)
                        row = {
                            "ref": [material["id"], material["rev"]],
                            "owner": material["owner"],
                            "usable": usable,
                            "reasons": reasons,
                            "service_event_id": served["id"] if served else None,
                        }
                        all_for_party.append(row)
                        if not usable:
                            excluded.append(row)
                party_rows[org] = {
                    "usable": self.usable_materials(case_id, mid, org, at_dt),
                    "all_related_versions": all_for_party,
                    "excluded_or_blocked": excluded,
                }
            proof_map[mid] = {"fact": matter["fact"], "parties": party_rows}

        checks = []
        for decision in case["decisions"]:
            for item in decision["items"]:
                for ref in item.get("cited_materials", []):
                    material = self._find_ref(case, ref)
                    usable, reasons, served = self.usable_ref(case, material, item["party"], parse_dt(item["decided_at"]))
                    checks.append(
                        {
                            "decision_id": decision["id"],
                            "item_id": item["id"],
                            "ref": ref,
                            "fact_match": item["fact"] in material.get("facts", []),
                            "usable_at_decision": usable,
                            "reasons": reasons,
                            "effective_service": served,
                            "sha256": material["sha256"],
                            "content_preview": material["content"][:120],
                        }
                    )

        return {
            "case": case,
            "proof_map": proof_map,
            "citation_checks": checks,
            "delivery_bundles": list(case["bundles"].values()),
            "effective_service_events": case["service_events"],
            "queried_at": iso(at_dt),
            "queried_by": actor["id"],
        }

    def warehouse_view(self, actor_id: str | None = None, at: Any = None) -> dict:
        if actor_id:
            self.require_roles(actor_id, "warehouse", "factory", "clerk", "reviewer")
        items = []
        restore_tasks = []
        executed = []
        for case in self.state["cases"].values():
            for decision in case.get("decisions", []):
                for item in decision.get("items", []):
                    row = {
                        "case_id": case["id"],
                        "decision_id": decision["id"],
                        "item_id": item["id"],
                        "batch": case["subjects"][item["subject_id"]]["batch"],
                        "supplier": item["party"],
                        "subject_id": item["subject_id"],
                        "outcome": item["outcome"],
                        "execution_spec": item.get("execution_spec"),
                        "execution_status": item.get("execution_status"),
                        "independently_executable": item.get("independently_executable"),
                        "correction_state": item.get("correction_state"),
                        "decided_at": decision.get("at"),
                        "facts": [item["fact"]],
                        "cited_materials": item.get("cited_materials", []),
                    }
                    if item.get("execution_status") == "executed":
                        row["physical_receipt_ids"] = item.get("physical_receipt_ids", [])
                        executed.append(row)
                    elif item.get("correction_state"):
                        continue
                    elif item["outcome"] == "uphold_reject" and item.get("independently_executable"):
                        if item.get("execution_status") in {"pending_execution", "execution_ordered", "partially_executed"}:
                            items.append(row)
                    elif item["outcome"] == "restore_receipt" and item.get("independently_executable"):
                        if item.get("execution_status") in {"pending_restore", "execution_ordered"}:
                            restore_tasks.append(row)
        holds = [h for case in self.state["cases"].values() for h in case.get("holds", []) if h.get("status") == "held"]
        receipts = [r for case in self.state["cases"].values() for r in case.get("physical_receipts", [])]
        return {
            "enforceable_rejections": items,
            "restore_tasks": restore_tasks,
            "executed_items": executed,
            "active_holds": holds,
            "physical_receipts": receipts,
            "note": "已执行且经送达范围纠正的事项只进入 remedies，不会重新出现在待转处。",
        }




