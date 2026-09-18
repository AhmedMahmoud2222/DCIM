from fastapi import APIRouter

from app.api.v1 import (
    alarms,
    auth,
    catalog,
    collectors,
    dashboard,
    discovery,
    equipment,
    floor_plans,
    health,
    integrations,
    locations,
    managed_assets,
    power,
    racks,
    settings,
    spatial,
    telemetry,
    users,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(alarms.router)
api_router.include_router(users.router)
api_router.include_router(locations.router)
api_router.include_router(managed_assets.router)
api_router.include_router(catalog.router)
api_router.include_router(racks.router)
api_router.include_router(equipment.router)
api_router.include_router(floor_plans.router)
api_router.include_router(spatial.router)
api_router.include_router(power.router)
api_router.include_router(dashboard.router)
api_router.include_router(collectors.router)
api_router.include_router(integrations.router)
api_router.include_router(discovery.router)
api_router.include_router(telemetry.router)
api_router.include_router(settings.router)
