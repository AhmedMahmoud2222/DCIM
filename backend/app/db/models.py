"""Single import point ensuring every ORM model is registered on Base.metadata before
Alembic autogenerate or `Base.metadata.create_all` runs."""

from app.domain.audit.models import AuditLog  # noqa: F401
from app.domain.auth.models import (  # noqa: F401
    Permission,
    RefreshToken,
    Role,
    RoleAssignment,
    RolePermission,
    User,
)
from app.domain.idempotency.models import IdempotencyKey  # noqa: F401
from app.domain.identity.models import ManagedAsset  # noqa: F401
from app.domain.location.models import (  # noqa: F401
    Building,
    City,
    Country,
    Floor,
    LocationType,
    Organization,
    Room,
    Site,
)
from app.domain.outbox.models import OutboxEvent  # noqa: F401
