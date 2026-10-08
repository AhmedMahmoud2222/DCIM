"""Single import point ensuring every ORM model is registered on Base.metadata before
Alembic autogenerate or `Base.metadata.create_all` runs."""

from app.domain.alarm.models import Alarm, AlarmRule  # noqa: F401
from app.domain.audit.models import AuditLog  # noqa: F401
from app.domain.auth.models import (  # noqa: F401
    Permission,
    RefreshToken,
    Role,
    RoleAssignment,
    RolePermission,
    User,
    UserGroup,
    UserGroupMember,
    UserGroupPermission,
    UserGroupRackAccess,
    UserGroupSiteAccess,
)
from app.domain.bulk_import.models import BulkImportJob, BulkImportRow  # noqa: F401
from app.domain.catalog.application_models import CatalogExtractionApplication  # noqa: F401
from app.domain.catalog.designer_models import (  # noqa: F401
    CatalogGraphic,
    CatalogGraphicMarker,
    CatalogModel,
    CatalogModelRevision,
    Manufacturer,
    MonitoringMetricTemplate,
    NetworkPortTemplate,
    PowerSupplyTemplate,
)
from app.domain.catalog.document_models import CatalogDocument, CatalogRevisionDocument  # noqa: F401
from app.domain.catalog.extraction_models import CatalogExtractionCandidate, CatalogExtractionJob  # noqa: F401
from app.domain.catalog.models import (  # noqa: F401
    EquipmentModel,
    EquipmentModelRevision,
    RackModel,
    RackModelRevision,
)
from app.domain.floorplan_import.models import (  # noqa: F401
    FloorPlanImportCandidate,
    FloorPlanImportDiagnostics,
    FloorPlanImportJob,
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
from app.domain.network.cable_models import Cable, CableEndpoint  # noqa: F401
from app.domain.network.discovery_models import DiscoveredNeighbor  # noqa: F401
from app.domain.network.pass_through_models import PortPassThrough, PortPassThroughMember  # noqa: F401
from app.domain.network.profile_models import DeviceProfile, ProfileMetricMapping, VendorProfile  # noqa: F401
from app.domain.outbox.models import OutboxEvent  # noqa: F401
from app.domain.physical.models import Equipment, Rack  # noqa: F401
from app.domain.physical.ports import EquipmentPort, EquipmentPowerInlet, PortConnection  # noqa: F401
from app.domain.placement.models import EquipmentPlacement, RackPlacement  # noqa: F401
from app.domain.power.analytics_models import PowerReportJob, PowerUtilizationSnapshot  # noqa: F401
from app.domain.power.models import (  # noqa: F401
    PDU,
    UPS,
    Generator,
    PDUOutlet,
    PowerCapacity,
    PowerConnection,
    PowerNode,
    PowerPanel,
    ProtectionDevice,
)
from app.domain.spatial.models import FloorPlan, SpatialLayer, SpatialObject  # noqa: F401
from app.domain.telemetry.mapping_models import PortTelemetryBinding, TelemetryLatestStatus  # noqa: F401
from app.domain.telemetry.models import (  # noqa: F401
    DailyTelemetryAggregate,
    IntegrationMetricMapping,
    MonitoringPolicy,
    TelemetryReading,
)
