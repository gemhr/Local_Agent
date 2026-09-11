"""LocalAgent HTTP 身份认证与最小授权 Owner。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import jwt
from fastapi import Request
from jwt import InvalidAudienceError, InvalidIssuerError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.persistence.models import ObjectOwnershipRow, RoleRow, UserRoleRow, UserRow

AUTH_MISSING_CREDENTIAL = "AUTH_MISSING_CREDENTIAL"
AUTH_INVALID_TOKEN = "AUTH_INVALID_TOKEN"
AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
AUTH_INVALID_ISSUER = "AUTH_INVALID_ISSUER"
AUTH_INVALID_AUDIENCE = "AUTH_INVALID_AUDIENCE"
AUTH_UNKNOWN_PRINCIPAL = "AUTH_UNKNOWN_PRINCIPAL"
AUTH_PRINCIPAL_DISABLED = "AUTH_PRINCIPAL_DISABLED"
AUTHORIZATION_FORBIDDEN = "AUTHORIZATION_FORBIDDEN"
AUTHORIZATION_OBJECT_NOT_OWNED = "AUTHORIZATION_OBJECT_NOT_OWNED"


@dataclass(frozen=True, slots=True)
class Principal:
    """服务器验证后的不可变身份。"""

    user_id: uuid.UUID
    subject: str
    roles: frozenset[str]
    token_id: str
    issued_at: datetime
    expires_at: datetime

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
            not isinstance(role, str) or role not in {"USER", "OPERATOR", "ADMIN"} for role in roles
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
        return Principal(user_id, subject, token_roles, token_id, _claim_datetime(payload, "iat"), _claim_datetime(payload, "exp"))


def require_role(principal: Principal, *roles: str) -> None:
    if not principal.roles.intersection(roles):
        raise AuthError(AUTHORIZATION_FORBIDDEN, 403)


def require_owned(principal: Principal, owner_id: str | uuid.UUID | None) -> None:
    if "ADMIN" not in principal.roles and (owner_id is None or str(owner_id) != str(principal.user_id)):
        raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)


class AuthorizationService:
    """HTTP object ownership 的唯一查询/绑定入口。"""

    def __init__(self, database: Any) -> None:
        self.database = database

    async def bind_new(self, principal: Principal, object_type: str, object_id: str) -> None:
        """创建时由认证身份绑定；冲突对象只能被原 owner 复用。"""
        try:
            async with self.database.transaction() as session:
                row = await session.get(
                    ObjectOwnershipRow, {"object_type": object_type, "object_id": object_id}
                )
                if row is None:
                    session.add(ObjectOwnershipRow(
                        object_type=object_type, object_id=object_id,
                        owner_user_id=principal.user_id,
                    ))
                    await session.flush()
                    return
                require_owned(principal, row.owner_user_id)
        except IntegrityError:
            # 并发首建时，下一次请求必须作为既有对象授权，而不是抢占 owner。
            await self.require_owner(principal, object_type, object_id)

    async def require_owner(self, principal: Principal, object_type: str, object_id: str) -> None:
        async with self.database.session() as session:
            row = await session.get(
                ObjectOwnershipRow, {"object_type": object_type, "object_id": object_id}
            )
        require_owned(principal, None if row is None else row.owner_user_id)

    async def require_owner_or_role(
        self, principal: Principal, object_type: str, object_id: str, *roles: str
    ) -> None:
        if principal.roles.intersection(roles):
            return
        await self.require_owner(principal, object_type, object_id)

    def require_owner_id(
        self, principal: Principal, owner_user_id: str | uuid.UUID | None
    ) -> None:
        """校验由业务表直接持有的 owner 列，不复制第二套授权策略。"""
        require_owned(principal, owner_user_id)


async def get_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise AuthError(AUTH_INVALID_TOKEN)
    return principal


__all__ = ["AuthError", "AuthService", "AuthorizationService", "Principal", "get_principal", "require_owned", "require_role"]
