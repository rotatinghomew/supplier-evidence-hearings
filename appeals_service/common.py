from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import threading
from typing import Any


class ServiceError(Exception):
    """可预期的业务错误。"""

    status = 400
    code = "service_error"

    def __init__(self, message: str | None = None, *, details: Any = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


class ValidationError(ServiceError):
    status = 400
    code = "validation_error"


class NotFoundError(ServiceError):
    status = 404
    code = "not_found"


class PermissionDeniedError(ServiceError):
    status = 403
    code = "permission_denied"


class ConflictError(ServiceError):
    status = 409
    code = "conflict"


def parse_dt(value: datetime | str | None, *, field: str = "time") -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"{field} 不是合法 ISO 8601 时间: {value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def as_ref(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return [str(value[0]), int(value[1])]
    raise ValidationError(f"材料引用必须是 [id, rev]，得到 {value!r}")


def refs_key(ref: list[Any] | tuple[Any, Any]) -> str:
    return f"{ref[0]}#r{int(ref[1])}"


def new_state() -> dict[str, Any]:
    return {
        "version": 1,
        "seq": 0,
        "orgs": [],
        "actors": {},
        "cases": {},
        "audit": [],
    }


class Store:
    """JSON 文件存储；path 为空时仅保存在内存。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        if self.path and self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.state = new_state()

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
