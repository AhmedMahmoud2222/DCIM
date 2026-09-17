from fastapi import APIRouter

from app.api.v1 import (
    auth,
    catalog,
    dashboard,
    equipment,
    floor_plans,
    health,
    locations,
    managed_assets,
    power,
    racks,
    spatial,
    users,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
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
