from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from core.auth import (
    AUTH_INVALID_TOKEN,
    AUTHORIZATION_FORBIDDEN,
    AuthError,
    AuthService,
    Principal,
    require_owned,
    require_role,
    require_scope,
)


class _Settings:
    jwt_public_key = "not-a-key"
    jwt_issuer = "localagent"
    jwt_audience = "localagent-api"
    jwt_allowed_algorithm = "EdDSA"
    jwt_clock_skew_seconds = 0


@pytest.mark.asyncio
async def test_malformed_and_algorithm_confusion_tokens_are_rejected() -> None:
    service = AuthService(object(), _Settings())
    with pytest.raises(AuthError) as missing:
        await service.authenticate(None)
    assert missing.value.code == "AUTH_MISSING_CREDENTIAL"
    token = jwt.encode({"alg": "none"}, key="", algorithm=None)
    with pytest.raises(AuthError) as invalid:
        await service.authenticate(f"Bearer {token}")
    assert invalid.value.code == AUTH_INVALID_TOKEN


def test_principal_is_immutable_and_ownership_is_server_side() -> None:
    user_id = uuid.uuid4()
    principal = Principal(user_id, str(user_id), frozenset({"USER"}), "jti", datetime.now(UTC), datetime.now(UTC) + timedelta(minutes=1))
    assert principal.authz_domain_id == str(user_id)
    require_owned(principal, user_id)
    with pytest.raises(AuthError) as denied:
        require_owned(principal, uuid.uuid4())
    assert denied.value.status_code == 404
    with pytest.raises(AuthError) as forbidden:
        require_role(principal, "ADMIN")
    assert forbidden.value.status_code == 403
    with pytest.raises((AttributeError, TypeError)):
        principal.roles = frozenset({"ADMIN"})  # type: ignore[misc]


def test_service_principal_cannot_use_admin_override_for_foreign_ownership() -> None:
    service_id = uuid.uuid4()
    principal = Principal(
        service_id,
        str(service_id),
        frozenset({"SERVICE", "ADMIN"}),
        "jti",
        datetime.now(UTC),
        datetime.now(UTC) + timedelta(minutes=1),
        "SERVICE",
        frozenset({"localagent:evaluation:execute"}),
    )

    require_owned(principal, service_id)
    with pytest.raises(AuthError) as denied:
        require_owned(principal, uuid.uuid4())
    assert denied.value.code == "AUTHORIZATION_OBJECT_NOT_OWNED"
    assert denied.value.status_code == 404


def test_evaluation_scope_requires_service_principal() -> None:
    user = Principal(
        uuid.uuid4(), "human", frozenset({"ADMIN"}), "jti", datetime.now(UTC),
        datetime.now(UTC) + timedelta(minutes=1), "HUMAN", frozenset(),
    )
    with pytest.raises(AuthError) as human_denied:
        require_scope(user, "localagent:evaluation:execute")
    assert human_denied.value.code == AUTHORIZATION_FORBIDDEN
    assert human_denied.value.status_code == 403

    service = Principal(
        uuid.uuid4(), "service", frozenset({"SERVICE"}), "jti", datetime.now(UTC),
        datetime.now(UTC) + timedelta(minutes=1), "SERVICE", frozenset(),
    )
    with pytest.raises(AuthError) as scope_denied:
        require_scope(service, "localagent:evaluation:execute")
    assert scope_denied.value.status_code == 403
