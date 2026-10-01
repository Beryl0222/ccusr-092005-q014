"""领域异常。所有违反业务规则的情况使用具体类型，HTTP 层映射状态码。"""
from __future__ import annotations


class SupplyError(Exception):
    """规则违反基类。"""


class ValidationError(SupplyError):
    """上报/请求结构不合法。"""


class ConstraintViolation(SupplyError):
    """触碰不可突破的约束（许可、换线、缺料、停机、检验）。"""


class OvercommitError(SupplyError):
    """产能重复锁定或超过可承诺量。"""


class NotFound(SupplyError):
    """对象不存在或对调用方不可见。"""


class PermissionDenied(SupplyError):
    """角色或数据归属不允许该操作。"""


class Conflict(SupplyError):
    """并发冲突：建议已失效 / 批次状态已变 / 幂等键重复但载荷不同。"""
