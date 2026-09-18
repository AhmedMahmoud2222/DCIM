"""Driver registry -- the one place that maps a protocol code to its driver class, so
`app/application/collector_service.py`'s polling orchestrator never branches on
`integration.integration_type` itself (master prompt §5: "Protocol/vendor-specific
behavior must not leak into core domain logic... do not implement `if vendor ==
"Schneider": ...` throughout the application"). Adding a new protocol means adding one
entry here, never touching the orchestrator."""

from app.application.drivers.base import AcquisitionResult, DriverConnectionError, ProtocolDriver
from app.application.drivers.icmp import ICMPDriver
from app.application.drivers.rest import RESTDriver
from app.application.drivers.snmp import SNMPDriver

DRIVER_REGISTRY: dict[str, type[ProtocolDriver]] = {
    "icmp": ICMPDriver,
    "rest": RESTDriver,
    "snmp": SNMPDriver,
}


def get_driver_class(protocol_code: str) -> type[ProtocolDriver]:
    driver_class = DRIVER_REGISTRY.get(protocol_code)
    if driver_class is None:
        raise ValueError(f"Unsupported protocol: {protocol_code!r}")
    return driver_class


__all__ = ["DRIVER_REGISTRY", "get_driver_class", "ProtocolDriver", "AcquisitionResult", "DriverConnectionError"]
