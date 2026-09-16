"""One-time bootstrap: create the first Administrator user. Never embedded in an Alembic
migration (credentials do not belong in migration history, §23 of the Phase 1 prompt:
"do not commit secrets"). Run after `alembic upgrade head`.

Usage:
    python scripts/create_admin.py --email admin@example.com --name "Admin User"
    (prompts for a password interactively; never accepts it as a CLI argument, which
    would leak it into shell history)
"""

import argparse
import asyncio
import getpass
import sys
import uuid

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.domain.auth.models import Role, RoleAssignment, User


async def main(email: str, full_name: str, password: str) -> None:
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()
        if existing is not None:
            print(f"User {email} already exists.", file=sys.stderr)
            sys.exit(1)

        role = (await db.execute(select(Role).where(Role.name == "Administrator"))).scalar_one_or_none()
        if role is None:
            print("Administrator role not found — run `alembic upgrade head` first.", file=sys.stderr)
            sys.exit(1)

        user = User(email=email.lower(), full_name=full_name, password_hash=hash_password(password))
        db.add(user)
        await db.flush()
        db.add(RoleAssignment(id=uuid.uuid4(), user_id=user.id, role_id=role.id, scope_type="global", scope_id=None))
        await db.commit()
        print(f"Created Administrator user {email} ({user.id})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True, dest="full_name")
    args = parser.parse_args()
    pw = getpass.getpass("Password: ")
    pw_confirm = getpass.getpass("Confirm password: ")
    if pw != pw_confirm:
        print("Passwords do not match.", file=sys.stderr)
        sys.exit(1)
    asyncio.run(main(args.email, args.full_name, pw))
