"""Admin API for the Kragen plugin subsystem."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from kragen.api.deps import UserId
from kragen.plugins.errors import PluginNotFoundError
from kragen.plugins.manager import get_plugin_manager

router = APIRouter(prefix="/admin/plugins", tags=["admin", "plugins"])


@router.get("")
async def list_plugins(user_id: UserId) -> dict[str, Any]:
    """List every discovered plugin with its current enabled state and specs."""
    _ = user_id
    manager = get_plugin_manager()
    items = manager.list_plugins()
    return {"total": len(items), "items": items}


@router.get("/{plugin_id}")
async def get_plugin(plugin_id: str, user_id: UserId) -> dict[str, Any]:
    """Return a single plugin descriptor (manifest, config, registered specs)."""
    _ = user_id
    try:
        return get_plugin_manager().get_plugin(plugin_id)
    except PluginNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Plugin not found: {exc}") from exc


@router.post("/{plugin_id}/enable")
async def enable_plugin(plugin_id: str, user_id: UserId) -> dict[str, Any]:
    """Enable a plugin and run its ``setup()`` immediately."""
    _ = user_id
    try:
        return get_plugin_manager().set_enabled(plugin_id, True)
    except PluginNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Plugin not found: {exc}") from exc


@router.post("/{plugin_id}/disable")
async def disable_plugin(plugin_id: str, user_id: UserId) -> dict[str, Any]:
    """Disable a plugin. Skill/MCP specs are dropped; backend routes require restart."""
    _ = user_id
    try:
        return get_plugin_manager().set_enabled(plugin_id, False)
    except PluginNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Plugin not found: {exc}") from exc


@router.put("/{plugin_id}/config")
async def update_plugin_config(
    plugin_id: str,
    user_id: UserId,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    """Replace a plugin's config dict and re-run ``setup()`` when enabled."""
    _ = user_id
    try:
        return get_plugin_manager().update_config(plugin_id, body)
    except PluginNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Plugin not found: {exc}") from exc
