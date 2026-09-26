"""One-time bootstrap: create the first Administrator user. Never embedded in an Alembic
migration (credentials do not belong in migration history, §23 of the Phase 1 prompt:
"do not commit secrets"). Run after `alembic upgrade head`.

Usage:
    python scripts/create_admin.py --email admin@example.com --name "Admin User"
    (prompts for a password interactively; never accepts it as a CLI argument, which
    would leak it into shell history)

For an unattended run — the CI browser-test fixture is the only current caller — pass
`--password-from-env VAR` to read the password from the environment instead of a TTY.
Still never a CLI argument: argv is world-readable through /proc and lands in shell
history and CI step traces, while an environment variable is neither echoed nor logged.
`--if-exists skip` makes the call idempotent so a re-run of a job does not fail on a
database that already carries the fixture.
"""

import argparse
import asyncio
import getpass
import os
import sys
import uuid

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.domain.auth.models import Role, RoleAssignment, User


async def main(email: str, full_name: str, password: str, *, skip_if_exists: bool = False) -> None:
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()
        if existing is not None:
            if skip_if_exists:
                print(f"User {email} already exists — leaving it as is.")
                return
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
    parser.add_argument(
        "--password-from-env",
        metavar="VAR",
        help="Read the password from this environment variable instead of prompting (unattended runs).",
    )
    parser.add_argument(
        "--if-exists",
        choices=("fail", "skip"),
        default="fail",
        help="What to do when the user already exists. Default: fail.",
    )
    args = parser.parse_args()
    if args.password_from_env:
        pw = os.environ.get(args.password_from_env, "")
        if not pw:
            print(f"{args.password_from_env} is unset or empty.", file=sys.stderr)
            sys.exit(1)
    else:
        pw = getpass.getpass("Password: ")
        pw_confirm = getpass.getpass("Confirm password: ")
        if pw != pw_confirm:
            print("Passwords do not match.", file=sys.stderr)
            sys.exit(1)
    asyncio.run(main(args.email, args.full_name, pw, skip_if_exists=args.if_exists == "skip"))
