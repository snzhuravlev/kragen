"""Exceptions for the Kragen plugin subsystem."""

from __future__ import annotations


class PluginError(Exception):
    """Base class for plugin-related errors."""


class PluginLoadError(PluginError):
    """Raised when a plugin entry point cannot be resolved or instantiated."""


class PluginConfigError(PluginError):
    """Raised when plugin configuration fails schema validation."""


class PluginAlreadyRegisteredError(PluginError):
    """Raised when two plugins attempt to register the same id."""


class PluginNotFoundError(PluginError):
    """Raised when an admin action references an unknown plugin id."""
