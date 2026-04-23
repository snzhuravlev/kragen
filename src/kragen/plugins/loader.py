"""Entry-point based plugin discovery.

Plugins expose themselves via the ``kragen.plugins`` entry-point group::

    # pyproject.toml of a plugin package
    [project.entry-points."kragen.plugins"]
    my_plugin = "my_plugin_pkg:plugin"

The referenced attribute can be:

* a zero-arg callable returning a ``KragenPlugin`` instance, or
* a class that can be instantiated without arguments, or
* an already-instantiated ``KragenPlugin``.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from typing import Any

from kragen.logging_config import get_logger
from kragen.plugins.base import KragenPlugin
from kragen.plugins.errors import PluginLoadError

logger = get_logger(__name__)

ENTRY_POINT_GROUP = "kragen.plugins"


@dataclass(frozen=True)
class DiscoveredPlugin:
    """Result of resolving a single entry point."""

    ep_name: str
    dist_name: str | None
    instance: KragenPlugin


def _instantiate(target: Any) -> KragenPlugin:
    """Normalize an entry-point target to a ``KragenPlugin`` instance."""
    candidate: Any = target
    if callable(candidate) and not isinstance(candidate, type):
        candidate = candidate()
    elif isinstance(candidate, type):
        candidate = candidate()

    if not isinstance(candidate, KragenPlugin):
        raise PluginLoadError(
            f"Entry point target {target!r} did not yield a KragenPlugin "
            "(missing `manifest` attribute or `setup(ctx)` method)."
        )
    return candidate


def discover_plugins() -> list[DiscoveredPlugin]:
    """Resolve every ``kragen.plugins`` entry point installed in the env."""
    found: list[DiscoveredPlugin] = []
    eps = importlib_metadata.entry_points(group=ENTRY_POINT_GROUP)

    for ep in eps:
        dist_name = getattr(ep.dist, "name", None) if getattr(ep, "dist", None) else None
        try:
            target = ep.load()
            instance = _instantiate(target)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "plugin_load_failed",
                entry_point=ep.name,
                distribution=dist_name,
                error=str(exc),
            )
            continue
        logger.info(
            "plugin_discovered",
            entry_point=ep.name,
            distribution=dist_name,
            plugin_id=instance.manifest.id,
            kind=instance.manifest.kind,
            version=instance.manifest.version,
        )
        found.append(
            DiscoveredPlugin(ep_name=ep.name, dist_name=dist_name, instance=instance)
        )
    return found
