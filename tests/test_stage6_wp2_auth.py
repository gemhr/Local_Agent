from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from core.auth import AUTH_INVALID_TOKEN, AuthError, AuthService, Principal, require_owned, require_role


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
