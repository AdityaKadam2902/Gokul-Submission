"""Discrepancies API router module (re-exported for direct module access)."""

from backend.app.api.routers.discrepancies import (
    DiscrepancyNotFound,
    OrganisationNotFound,
    router,
)

__all__ = [
    "DiscrepancyNotFound",
    "OrganisationNotFound",
    "router",
]
