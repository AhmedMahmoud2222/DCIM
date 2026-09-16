"""Minimal user/role administration (§13: authorization is enforced here server-side via
require_permission, never by hiding a button). Full role/permission management UI is not
a Phase 1 deliverable — this exists so an Administrator can onboard users and assign the
seeded default roles."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.audit_service import write_audit_log
from app.application.rbac import require_permission
from app.core.errors import NotFoundError
from app.core.security import hash_password
from app.domain.auth.models import Role, RoleAssignment, User

router = APIRouter(prefix="/users", tags=["users"])


class UserIn(BaseModel):
    email: EmailStr
    full_name: str
    password: str
    role_name: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


@router.post("", response_model=UserOut, status_code=201)
async def create_user(
    body: UserIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("user:manage")),
) -> User:
    role = (await db.execute(select(Role).where(Role.name == body.role_name))).scalar_one_or_none()
    if role is None:
        raise NotFoundError(f"Role {body.role_name!r} not found.")

    user = User(email=body.email.lower(), full_name=body.full_name, password_hash=hash_password(body.password))
    db.add(user)
    await db.flush()
    db.add(RoleAssignment(user_id=user.id, role_id=role.id, scope_type="global", scope_id=None))
    await db.flush()

    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="user.create",
        entity_type="user",
        entity_id=user.id,
        request_id=getattr(request.state, "request_id", None),
        correlation_id=getattr(request.state, "correlation_id", None),
        after={"email": user.email, "role": body.role_name},
    )
    await db.commit()
    await db.refresh(user)
    return user
