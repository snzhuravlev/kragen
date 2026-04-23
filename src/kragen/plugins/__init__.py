"""Kragen plugin subsystem: public API for third-party plugins.

See ``docs/PLUGINS.md`` for the contract and examples.
"""

from kragen.plugins.base import (
    BackendSpec,
    BasePlugin,
    ChannelSpec,
    KragenPlugin,
    MCPServerSpec,
    PluginKind,
    PluginManifest,
    SkillSpec,
)
from kragen.plugins.context import PluginContext
from kragen.plugins.errors import (
    PluginAlreadyRegisteredError,
    PluginConfigError,
    PluginError,
    PluginLoadError,
    PluginNotFoundError,
)
from kragen.plugins.manager import PluginManager, get_plugin_manager

__all__ = [
    "BackendSpec",
    "BasePlugin",
    "ChannelSpec",
    "KragenPlugin",
    "MCPServerSpec",
    "PluginContext",
    "PluginKind",
    "PluginManager",
    "PluginManifest",
    "SkillSpec",
    "PluginAlreadyRegisteredError",
    "PluginConfigError",
    "PluginError",
    "PluginLoadError",
    "PluginNotFoundError",
    "get_plugin_manager",
]
