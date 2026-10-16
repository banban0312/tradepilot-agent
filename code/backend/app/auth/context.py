"""可信请求身份及与客户端字段无关的授权判断。"""

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException

BUSINESS_OPERATOR = "business_operator"
BUSINESS_APPROVER = "business_approver"
SYSTEM_ADMIN = "system_admin"


@dataclass(frozen=True, slots=True)
class AuthContext:
    """由服务端会话解析出的身份；不得从模型参数或请求 body 构造。"""

    user_id: UUID
    login: str
    roles: frozenset[str]
    department_ids: frozenset[UUID]


def require_role(context: AuthContext, role: str) -> None:
    """按明确角色授权；系统管理员不会隐式获得经办或审批能力。"""
    if role not in context.roles:
        raise HTTPException(
            status_code=403,
            detail={"code": "ACCESS_DENIED", "message": "当前账号没有执行此操作的权限"},
        )


def require_department_scope(context: AuthContext, department_id: UUID) -> None:
    """只按会话内的服务端授权范围判断部门，不采信客户端自报部门。"""
    if department_id not in context.department_ids:
        raise HTTPException(
            status_code=404,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "资源不存在"},
        )


def require_business_access(context: AuthContext, department_id: UUID) -> None:
    """业务数据访问必须同时具备部门范围和经办/审批角色。"""
    require_department_scope(context, department_id)
    if not context.roles.intersection({BUSINESS_OPERATOR, BUSINESS_APPROVER}):
        raise HTTPException(
            status_code=404,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "资源不存在"},
        )
