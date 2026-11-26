"""兼容性别名；正式实现位于 :mod:`appeals_service.service`。"""

from appeals_service.service import AppealsService, ServiceError

__all__ = ["AppealsService", "ServiceError"]
