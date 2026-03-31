from datetime import datetime, UTC

from pydantic import BaseModel, Field


class ScreenSignature(BaseModel):
    activity: str | None = None
    key_elements: list[str] = []


class LessonContext(BaseModel):
    goal: str = ""
    screen_signature: ScreenSignature = ScreenSignature()
    action_attempted: str = ""
    what_happened: str = ""


class PathStep(BaseModel):
    """A single step in a successful navigation path."""

    action: str = ""  # Tool name: tap, launch_app, open_link, back
    target_text: str | None = None  # Text of the tapped element
    target_resource_id: str | None = None  # resource_id if available
    result: str = ""  # Brief description of what happened


class LessonEntry(BaseModel):
    id: str
    type: str  # "mistake" | "strategy" | "ui_mapping" | "success_path"
    category: str  # "navigation" | "messaging" | "search" | "media" | "settings" | "general"
    summary: str
    context: LessonContext = LessonContext()
    lesson: str = ""
    suggested_strategy: str = ""
    confidence: float = 0.5
    occurrences: int = 1
    applied_success: int = 0
    applied_failure: int = 0
    created: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
    path: list[PathStep] | None = None  # Only populated for type="success_path"
    deprecated: bool = False
    app_version: str | None = None  # Phase 3: version when lesson was recorded


class AppMeta(BaseModel):
    """Per-app metadata stored in _meta.json."""

    package: str
    display_name: str
    app_version: str | None = None
    version_source: str | None = None
    last_verified: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    notes: str | None = None


class AppIndexEntry(BaseModel):
    display_name: str
    lesson_count: int
    last_updated: str


class AppIndex(BaseModel):
    apps: dict[str, AppIndexEntry] = {}
