"""LocalAgent HTTP 身份认证与最小授权 Owner。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import jwt
from fastapi import Request
from jwt import InvalidAudienceError, InvalidIssuerError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.persistence.models import (
    BusinessReviewRow, ObjectOwnershipRow, RoleRow,
    Stage8ExternalExecutionJobRow, Stage8GeneratedCaseArtifactRow,
    Stage8TestPlanRow, Stage8TicketContinuationRow, UserRoleRow, UserRow,
)

AUTH_MISSING_CREDENTIAL = "AUTH_MISSING_CREDENTIAL"
AUTH_INVALID_TOKEN = "AUTH_INVALID_TOKEN"
AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
AUTH_INVALID_ISSUER = "AUTH_INVALID_ISSUER"
AUTH_INVALID_AUDIENCE = "AUTH_INVALID_AUDIENCE"
AUTH_UNKNOWN_PRINCIPAL = "AUTH_UNKNOWN_PRINCIPAL"
AUTH_PRINCIPAL_DISABLED = "AUTH_PRINCIPAL_DISABLED"
AUTHORIZATION_FORBIDDEN = "AUTHORIZATION_FORBIDDEN"
AUTHORIZATION_OBJECT_NOT_OWNED = "AUTHORIZATION_OBJECT_NOT_OWNED"
EVALUATION_EXECUTE_SCOPE = "localagent:evaluation:execute"


class AuthorizationAction(str, Enum):
    READ = "READ"
    MUTATE = "MUTATE"
    CANCEL = "CANCEL"
    APPROVE = "APPROVE"
    PROCESS = "PROCESS"
    RESUME = "RESUME"
    SUBSCRIBE = "SUBSCRIBE"


@dataclass(frozen=True, slots=True)
class Principal:
    """服务器验证后的不可变身份。"""

    user_id: uuid.UUID
    subject: str
    roles: frozenset[str]
    token_id: str
    issued_at: datetime
    expires_at: datetime
    principal_kind: str = "HUMAN"
    scopes: frozenset[str] = frozenset()
    tenant_id: str = ""

    @property
    def authz_domain_id(self) -> str:
        return str(self.user_id)


class AuthError(Exception):
    def __init__(self, code: str, status_code: int = 401) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _claim_datetime(payload: dict[str, Any], name: str) -> datetime:
    value = payload.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise AuthError(AUTH_INVALID_TOKEN)
    return datetime.fromtimestamp(value, tz=UTC)


class AuthService:
    """JWT verification plus PostgreSQL principal lookup。"""

    def __init__(self, database: Any, settings: Any) -> None:
        self.database = database
        self.public_key = settings.jwt_public_key
        self.issuer = settings.jwt_issuer
        self.audience = settings.jwt_audience
        self.algorithm = settings.jwt_allowed_algorithm
        self.clock_skew_seconds = settings.jwt_clock_skew_seconds

    async def authenticate(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthError(AUTH_MISSING_CREDENTIAL)
        token = authorization[7:].strip()
        if not token:
            raise AuthError(AUTH_MISSING_CREDENTIAL)
        try:
            if jwt.get_unverified_header(token).get("alg") != self.algorithm:
                raise AuthError(AUTH_INVALID_TOKEN)
            payload = jwt.decode(
                token, self.public_key, algorithms=[self.algorithm],
                issuer=self.issuer, audience=self.audience,
                leeway=self.clock_skew_seconds,
                options={"require": ["iss", "aud", "sub", "roles", "jti", "iat", "nbf", "exp"]},
            )
        except AuthError:
            raise
        except jwt.ExpiredSignatureError:
            raise AuthError(AUTH_TOKEN_EXPIRED) from None
        except InvalidIssuerError:
            raise AuthError(AUTH_INVALID_ISSUER) from None
        except InvalidAudienceError:
            raise AuthError(AUTH_INVALID_AUDIENCE) from None
        except Exception:
            raise AuthError(AUTH_INVALID_TOKEN) from None
        subject, roles, token_id = payload.get("sub"), payload.get("roles"), payload.get("jti")
        if not isinstance(subject, str) or not isinstance(token_id, str) or not token_id:
            raise AuthError(AUTH_INVALID_TOKEN)
        if not isinstance(roles, list) or not roles or any(
            not isinstance(role, str) or role not in {"USER", "OPERATOR", "ADMIN", "SERVICE"} for role in roles
        ):
            raise AuthError(AUTH_INVALID_TOKEN)
        try:
            user_id = uuid.UUID(subject)
        except ValueError:
            raise AuthError(AUTH_INVALID_TOKEN) from None
        async with self.database.session() as session:
            user = await session.scalar(select(UserRow).where(UserRow.id == user_id, UserRow.subject == subject))
            if user is None:
                raise AuthError(AUTH_UNKNOWN_PRINCIPAL)
            if user.disabled_at is not None:
                raise AuthError(AUTH_PRINCIPAL_DISABLED)
            db_roles = await session.scalars(select(RoleRow.code).join(UserRoleRow, UserRoleRow.role_id == RoleRow.id).where(UserRoleRow.user_id == user_id))
            assigned_roles = frozenset(db_roles.all())
        token_roles = frozenset(roles)
        if not token_roles.issubset(assigned_roles):
            raise AuthError(AUTH_INVALID_TOKEN)
        principal_kind = user.principal_kind
        if principal_kind not in {"HUMAN", "SERVICE"}:
            raise AuthError(AUTH_INVALID_TOKEN)
        if principal_kind == "SERVICE" and "SERVICE" not in assigned_roles:
            raise AuthError(AUTH_INVALID_TOKEN)
        raw_scopes = payload.get("scopes", [])
        if not isinstance(raw_scopes, list) or any(not isinstance(scope, str) for scope in raw_scopes):
            raise AuthError(AUTH_INVALID_TOKEN)
        scopes = frozenset(raw_scopes)
        if principal_kind == "SERVICE":
            configured_scopes = frozenset(user.service_scopes or [])
            if not scopes.issubset(configured_scopes):
                raise AuthError(AUTH_INVALID_TOKEN)
        token_tenant = payload.get("tenant_id")
        if token_tenant is not None and (not isinstance(token_tenant, str) or token_tenant != user.tenant_id):
            raise AuthError(AUTH_INVALID_TOKEN)
        return Principal(user_id, subject, token_roles, token_id, _claim_datetime(payload, "iat"), _claim_datetime(payload, "exp"), principal_kind, scopes, user.tenant_id)


def require_role(principal: Principal, *roles: str) -> None:
    if not principal.roles.intersection(roles):
        raise AuthError(AUTHORIZATION_FORBIDDEN, 403)


def require_scope(principal: Principal, scope: str) -> None:
    """校验 service principal 的窄 endpoint scope；human 不得走 service path。"""
    if principal.principal_kind != "SERVICE" or scope not in principal.scopes:
        raise AuthError(AUTHORIZATION_FORBIDDEN, 403)


def require_owned(principal: Principal, owner_id: str | uuid.UUID | None) -> None:
    """仅校验精确 owner；ADMIN 必须通过 tenant-aware object policy。"""
    if owner_id is None or str(owner_id) != str(principal.user_id):
        raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)


class ObjectAuthorizationService:
    """对象级授权唯一 Owner；不拥有任何 Runtime 或业务状态。"""

    def __init__(self, database: Any) -> None:
        self.database = database

    @staticmethod
    def _require_tenant(principal: Principal) -> str:
        if not principal.tenant_id:
            raise AuthError(AUTHORIZATION_FORBIDDEN, 403)
        return principal.tenant_id

    @staticmethod
    def _apply_policy(
        principal: Principal,
        row: ObjectOwnershipRow | None,
        action: AuthorizationAction,
        required_scope: str | None,
    ) -> None:
        tenant_id = ObjectAuthorizationService._require_tenant(principal)
        if row is None or row.tenant_id != tenant_id:
            raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)
        if action in {AuthorizationAction.RESUME, AuthorizationAction.SUBSCRIBE}:
            raise AuthError(AUTHORIZATION_FORBIDDEN, 403)
        if principal.principal_kind == "SERVICE":
            if (
                action not in {
                    AuthorizationAction.READ,
                    AuthorizationAction.MUTATE,
                    AuthorizationAction.CANCEL,
                    AuthorizationAction.PROCESS,
                }
                or required_scope is None
                or required_scope not in principal.scopes
            ):
                raise AuthError(AUTHORIZATION_FORBIDDEN, 403)
            return
        if required_scope is not None:
            raise AuthError(AUTHORIZATION_FORBIDDEN, 403)
        is_admin = principal.principal_kind == "HUMAN" and "ADMIN" in principal.roles
        if not is_admin and str(row.owner_user_id) != str(principal.user_id):
            raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)

    async def _resolve_ownership(
        self, session: Any, object_type: str, object_id: str
    ) -> ObjectOwnershipRow | None:
        row = await session.get(
            ObjectOwnershipRow, {"object_type": object_type, "object_id": object_id}
        )
        if row is not None or object_type == "MISSION":
            return row
        child_models = {
            "REVIEW": BusinessReviewRow,
            "TEST_PLAN": Stage8TestPlanRow,
            "ARTIFACT": Stage8GeneratedCaseArtifactRow,
            "GENERATED_CASE_ARTIFACT": Stage8GeneratedCaseArtifactRow,
            "EXTERNAL_EXECUTION_JOB": Stage8ExternalExecutionJobRow,
            "TICKET_CONTINUATION": Stage8TicketContinuationRow,
        }
        model = child_models.get(object_type)
        child = await session.get(model, object_id) if model is not None else None
        if child is None or not hasattr(child, "mission_id"):
            return None
        return await session.get(
            ObjectOwnershipRow,
            {"object_type": "MISSION", "object_id": child.mission_id},
        )

    async def _require_existing_binding(
        self, principal: Principal, object_type: str, object_id: str
    ) -> None:
        tenant_id = self._require_tenant(principal)
        async with self.database.session() as session:
            row = await session.get(
                ObjectOwnershipRow,
                {"object_type": object_type, "object_id": object_id},
            )
        if row is None or row.tenant_id != tenant_id:
            raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)
        is_admin = principal.principal_kind == "HUMAN" and "ADMIN" in principal.roles
        if not is_admin and str(row.owner_user_id) != str(principal.user_id):
            raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)

    async def bind_new(self, principal: Principal, object_type: str, object_id: str) -> None:
        """创建时由认证身份绑定；冲突对象只能被原 owner 复用。"""
        tenant_id = self._require_tenant(principal)
        try:
            async with self.database.transaction() as session:
                row = await session.get(
                    ObjectOwnershipRow, {"object_type": object_type, "object_id": object_id}
                )
                if row is None:
                    session.add(ObjectOwnershipRow(
                        object_type=object_type, object_id=object_id,
                        owner_user_id=principal.user_id, tenant_id=tenant_id,
                    ))
                    await session.flush()
                    return
                if row.tenant_id != tenant_id:
                    raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)
                is_admin = (
                    principal.principal_kind == "HUMAN"
                    and "ADMIN" in principal.roles
                )
                if not is_admin and str(row.owner_user_id) != str(principal.user_id):
                    raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)
        except IntegrityError:
            # 并发首建时，下一次请求必须作为既有对象授权，而不是抢占 owner。
            await self._require_existing_binding(principal, object_type, object_id)

    async def require_owner(self, principal: Principal, object_type: str, object_id: str) -> None:
        await self.authorize(
            principal, object_type, object_id, AuthorizationAction.READ
        )

    async def authorize(
        self, principal: Principal, object_type: str, object_id: str,
        action: AuthorizationAction | str, *, required_scope: str | None = None,
    ) -> None:
        """按 Tenant → owner/role → scope → action policy 顺序授权。

        未找到、跨租户和无权访问均返回 404，避免对象 ID 枚举。
        """
        try:
            action = AuthorizationAction(action)
        except ValueError as exc:
            raise AuthError(AUTHORIZATION_FORBIDDEN, 403) from exc
        async with self.database.session() as session:
            row = await self._resolve_ownership(session, object_type, object_id)
        self._apply_policy(principal, row, action, required_scope)

    async def authorize_external_execution(
        self,
        principal: Principal,
        execution_id: str,
        action: AuthorizationAction | str,
        *,
        required_scope: str | None = None,
    ) -> str:
        """按 provider execution identity 解析并授权唯一 canonical Job。"""
        try:
            action = AuthorizationAction(action)
        except ValueError as exc:
            raise AuthError(AUTHORIZATION_FORBIDDEN, 403) from exc
        async with self.database.session() as session:
            job = await session.scalar(
                select(Stage8ExternalExecutionJobRow).where(
                    Stage8ExternalExecutionJobRow.execution_id == execution_id
                )
            )
            row = (
                None
                if job is None
                else await self._resolve_ownership(
                    session, "MISSION", job.mission_id
                )
            )
        canonical_job_id = None if job is None else job.job_id
        self._apply_policy(principal, row, action, required_scope)
        assert canonical_job_id is not None
        return canonical_job_id


    async def require_owner_or_role(
        self, principal: Principal, object_type: str, object_id: str, *roles: str
    ) -> None:
        # Role checks never bypass the tenant boundary; role policy is applied
        # only after the object has been resolved in the same tenant.
        await self.require_owner(principal, object_type, object_id)


# Existing callers keep importing this name; the implementation has one owner.
AuthorizationService = ObjectAuthorizationService


async def get_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise AuthError(AUTH_INVALID_TOKEN)
    return principal


__all__ = ["AuthorizationAction", "EVALUATION_EXECUTE_SCOPE", "AuthError", "AuthService", "AuthorizationService", "ObjectAuthorizationService", "Principal", "get_principal", "require_owned", "require_role", "require_scope"]
