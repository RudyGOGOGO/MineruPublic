from fastapi import APIRouter
from pydantic import BaseModel, Field

from deerflow.config import get_app_config

router = APIRouter(prefix="/api", tags=["sandboxes"])


class SandboxResponse(BaseModel):
    """Response model for a sandbox configuration."""

    name: str = Field(..., description="Unique identifier for the sandbox")
    display_name: str | None = Field(None, description="Human-readable name")


class SandboxesListResponse(BaseModel):
    """Response model for listing available sandboxes."""

    sandboxes: list[SandboxResponse]


@router.get(
    "/sandboxes",
    response_model=SandboxesListResponse,
    summary="List Available Sandboxes",
    description="Retrieve a list of all available sandbox configurations that users can switch between.",
)
async def list_sandboxes() -> SandboxesListResponse:
    """List all available sandbox configurations.

    Returns sandbox names and display names suitable for frontend display.
    If named sandboxes are configured, returns those.
    Otherwise returns a single "default" entry.

    Returns:
        A list of all available sandboxes.
    """
    config = get_app_config()
    available = config.get_available_sandboxes()
    sandboxes = [
        SandboxResponse(
            name=name,
            display_name=cfg.display_name or name.capitalize(),
        )
        for name, cfg in available.items()
    ]
    return SandboxesListResponse(sandboxes=sandboxes)
