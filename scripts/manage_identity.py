"""受控 development/test 身份管理命令；不暴露 HTTP token endpoint。"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy import select

from core.persistence import Database, DatabaseConfig, RoleRow, UserRoleRow, UserRow
from core.settings import EnvironmentProfile, Settings, load_identity_issuer_settings


async def _run(args: argparse.Namespace) -> None:
    settings = Settings.load()
    if settings.environment_profile is EnvironmentProfile.PRODUCTION:
        raise SystemExit("identity management is limited to LOCAL/TEST")
    database = Database(DatabaseConfig.from_settings(settings))
    try:
        if args.command == "create-user":
            user_id = uuid.uuid4()
            role_id = {"USER": "00000000-0000-0000-0000-000000000001", "OPERATOR": "00000000-0000-0000-0000-000000000002", "ADMIN": "00000000-0000-0000-0000-000000000003"}[args.role]
            async with database.transaction() as session:
                session.add(UserRow(id=user_id, subject=str(user_id), display_name=args.display_name))
                # UserRoleRow 没有 ORM relationship；显式 flush 保证 FK parent
                # 在同一 application-owned transaction 内先于关联行写入。
                await session.flush()
                session.add(UserRoleRow(user_id=user_id, role_id=uuid.UUID(role_id)))
            print(user_id)
            return
        private_key, issuer, audience, algorithm = load_identity_issuer_settings()
        if not private_key or algorithm != "EdDSA":
            raise SystemExit("LOCAL_AGENT_JWT_PRIVATE_KEY and EdDSA are required")
        async with database.session() as session:
            user = await session.scalar(select(UserRow).where(UserRow.id == uuid.UUID(args.user_id)))
            if user is None:
                raise SystemExit("unknown user")
            roles = await session.scalars(select(RoleRow.code).join(UserRoleRow, UserRoleRow.role_id == RoleRow.id).where(UserRoleRow.user_id == user.id))
            role_values = list(roles.all())
        now = datetime.now(UTC)
        print(jwt.encode({"iss": issuer, "aud": audience, "sub": str(user.id), "roles": role_values, "jti": uuid.uuid4().hex, "iat": now, "nbf": now, "exp": now + timedelta(seconds=args.ttl)}, private_key, algorithm=algorithm))
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-user")
    create.add_argument("--display-name", required=True)
    create.add_argument("--role", choices=("USER", "OPERATOR", "ADMIN"), default="USER")
    issue = sub.add_parser("issue-test-token")
    issue.add_argument("user_id")
    issue.add_argument("--ttl", type=int, default=900)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
