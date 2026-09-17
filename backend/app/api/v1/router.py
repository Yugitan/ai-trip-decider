"""API v1 路由聚合。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import admin, catalog, health, public, reports, trips

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(catalog.router)
api_router.include_router(trips.router)
api_router.include_router(public.router)
api_router.include_router(reports.router)
api_router.include_router(admin.router)
