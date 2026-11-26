"""拒收申诉办理服务。

仅依赖 Python 标准库。核心领域逻辑在 :mod:`appeals_service.service`，
HTTP 适配层在 :mod:`appeals_service.api`。
"""

from .service import AppealsService, ServiceError

__all__ = ["AppealsService", "ServiceError"]
__version__ = "1.0.0"
