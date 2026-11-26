"""仅依赖标准库的 JSON HTTP 适配层。"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import json
import re
from typing import Any

from .common import NotFoundError, ServiceError
from .service import AppealsService


def create_handler(service: AppealsService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AppealsService/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            # 测试时保持输出安静；需要访问日志可由外部 WSGI/反向代理记录。
            return

        def _actor(self, body: dict | None = None) -> str | None:
            header = self.headers.get("X-Actor")
            if header:
                return header
            query = parse_qs(urlparse(self.path).query)
            if query.get("actor"):
                return query["actor"][0]
            return (body or {}).get("actor_id") or (body or {}).get("actor")

        def _send(self, status: int, payload: Any) -> None:
            raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ServiceError("请求体不是合法 JSON") from exc
            if not isinstance(value, dict):
                raise ServiceError("请求体必须是 JSON 对象")
            return value

        def _handle(self, method: str) -> None:
            try:
                body = self._body() if method == "POST" else {}
                actor = self._actor(body)
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                path = [p for p in url.path.split("/") if p]
                result, status = route(service, method, path, query, body, actor)
                self._send(status, result)
            except ServiceError as exc:
                self._send(exc.status, exc.to_dict())
            except Exception as exc:  # noqa: BLE001 - HTTP 边界需要 JSON 化
                self._send(500, {"error": "internal_error", "message": str(exc)})

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

    return Handler


def _ok(value: Any, status: int = 200):
    return value, status


def route(
    svc: AppealsService,
    method: str,
    path: list[str],
    query: dict[str, str],
    body: dict,
    actor: str | None,
):
    if path == [] and method == "GET":
        return _ok({
            "service": "rejection-appeals",
            "version": "1.0.0",
            "docs": "README.md",
            "endpoints": [
                "/health", "/api/cases", "/api/cases/{id}", "/api/cases/{id}/materials",
                "/api/cases/{id}/deliveries", "/api/cases/{id}/receipts",
                "/api/cases/{id}/answers", "/api/cases/{id}/decisions",
                "/api/cases/{id}/verification", "/api/warehouse", "/api/remedies",
            ],
        })

    # /health
    if path == ["health"] and method == "GET":
        return _ok({"ok": True, "service": "rejection-appeals", "version": "1.0.0"})

    # /api/orgs, /api/actors
    if path == ["api", "orgs"] and method == "POST":
        return _ok(svc.register_org(body["id"], body.get("name"), body.get("kind", "party")), 201)
    if path == ["api", "actors"] and method == "POST":
        return _ok(
            svc.register_actor(
                body["id"], body["org"], name=body.get("name"), roles=body.get("roles")
            ),
            201,
        )

    if path == ["api", "cases"] and method == "GET":
        return _ok({"cases": svc.list_cases(actor, query.get("at"))})
    if path == ["api", "cases"] and method == "POST":
        return _ok(svc.create_case(body, actor), 201)

    if len(path) >= 3 and path[:2] == ["api", "cases"]:
        cid = path[2]
        rest = path[3:]

        if rest == [] and method == "GET":
            record = svc.actor(actor) if actor else None
            if svc._full_evidence_access(record):
                return _ok(svc.verification_view(cid, actor, query.get("at")))
            org = query.get("org") or (record["org"] if record else None)
            if not org:
                raise ServiceError("供货商/厂方视图需要 X-Actor 或 org")
            return _ok(svc.party_view(cid, org, query.get("at"), actor=record))

        if rest == ["summary"] and method == "GET":
            return _ok(svc.case_summary(cid, query.get("at")))
        if rest == ["appeals"] and method == "POST":
            return _ok(svc.file_appeal(cid, body, actor), 201)
        if rest == ["accept"] and method == "POST":
            return _ok(svc.accept_case(cid, body, actor))
        m = re.fullmatch(r"matters/([^/]+)/configure", "/".join(rest))
        if m and method == "POST":
            return _ok(svc.configure_matter(cid, m.group(1), body, actor))
        if rest == ["mandates"] and method == "POST":
            return _ok(svc.add_mandate(cid, body, actor), 201)

        if rest == ["materials"] and method == "POST":
            return _ok(svc.upload_material(cid, body, actor), 201)
        if rest == ["materials"] and method == "GET":
            record = svc.actor(actor) if actor else None
            if svc._full_evidence_access(record):
                return _ok({"materials": list(svc._case(cid)["materials"].values())})
            org = query.get("org") or record["org"]
            view = svc.party_view(cid, org, query.get("at"), actor=record)
            return _ok({"materials": view["materials"]})

        m = re.fullmatch(r"materials/([^/]+)/versions/(\d+)", "/".join(rest))
        if m and method == "GET":
            return _ok(svc.get_material_content(cid, m.group(1), int(m.group(2)), actor, query.get("at")))
        m = re.fullmatch(r"materials/([^/]+)/versions/(\d+)/confirm", "/".join(rest))
        if m and method == "POST":
            return _ok(svc.confirm_material(cid, m.group(1), int(m.group(2)), body, actor))

        if rest == ["withdrawals"] and method == "POST":
            return _ok(svc.withdraw_consent(cid, body, actor), 201)

        if rest == ["deliveries"] and method == "POST":
            return _ok(svc.create_delivery(cid, body, actor), 201)
        if rest == ["deliveries"] and method == "GET":
            return _ok({"deliveries": svc.list_deliveries(cid, actor)})
        m = re.fullmatch(r"deliveries/([^/]+)", "/".join(rest))
        if m and method == "GET":
            return _ok(svc.delivery_view(cid, m.group(1)))
        m = re.fullmatch(r"deliveries/([^/]+)/send", "/".join(rest))
        if m and method == "POST":
            return _ok(svc.send_delivery(cid, m.group(1), body, actor))
        m = re.fullmatch(r"deliveries/([^/]+)/timeout", "/".join(rest))
        if m and method == "POST":
            return _ok(svc.record_timeout_check(cid, m.group(1), body, actor))

        if rest == ["receipts"] and method == "POST":
            return _ok(svc.ingest_receipt(cid, body, actor), 201)
        if rest == ["served"] and method == "GET":
            return _ok(svc.served_and_openable(cid, actor, at=query.get("at")))

        if rest == ["answers"] and method == "POST":
            return _ok(svc.file_answer(cid, body, actor), 201)
        if rest == ["waivers"] and method == "POST":
            return _ok(svc.waive_period(cid, body, actor), 201)
        if rest == ["decisions"] and method == "POST":
            return _ok(svc.make_decision(cid, body, actor), 201)
        if rest == ["verification"] and method == "GET":
            return _ok(svc.verification_view(cid, actor, query.get("at")))
        if rest == ["remands"] and method == "GET":
            return _ok({"remands": svc.list_remands(cid, actor)})
        if rest == ["corrections"] and method == "POST":
            return _ok(svc.correct_service_scope(cid, body, actor), 201)

        if rest == ["executions"] and method == "POST":
            return _ok(svc.create_execution(cid, body, actor), 201)
        m = re.fullmatch(r"executions/([^/]+)/receipts", "/".join(rest))
        if m and method == "POST":
            return _ok(svc.record_physical_receipt(cid, m.group(1), body, actor), 201)

    if path == ["api", "warehouse"] and method == "GET":
        return _ok(svc.warehouse_view(actor, query.get("at")))
    if path == ["api", "remedies"] and method == "GET":
        return _ok({"remedies": svc.list_remedies(actor)})

    raise NotFoundError(f"没有路由: {method} /{'/'.join(path)}")


def serve(service: AppealsService | None = None, host: str = "127.0.0.1", port: int = 8080):
    service = service or AppealsService()
    httpd = ThreadingHTTPServer((host, port), create_handler(service))
    return httpd
