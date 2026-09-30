"""领域异常。消息面向调度员/监管人员，使用中文。"""

from __future__ import annotations


class ServiceError(Exception):
    """业务错误基类，HTTP 层映射为 4xx。"""

    http_status = 400


class ValidationError(ServiceError):
    """上报或指令未通过台账/字段校验。"""

    http_status = 422


class ConflictError(ServiceError):
    """并发冲突：产能/批次已被其他承诺占用，或状态不允许该操作。"""

    http_status = 409


class NotFoundError(ServiceError):
    http_status = 404


class PermissionError(ServiceError):  # noqa: A001 - 领域内有意遮蔽内建名
    http_status = 403


class IdempotencyReplayed(ServiceError):
    """同一幂等键重复提交且载荷不一致。"""

    http_status = 409
