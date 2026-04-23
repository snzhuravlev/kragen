"""Public plugin contracts: manifest, kinds, protocol, registrable specs.

All plugins register one or more *specs* (skill / MCP server / backend router /
channel) via ``PluginContext`` during ``setup()``. Kragen core consumes specs
through the ``PluginManager`` — plugins never touch the runtime (DB engine,
orchestrator internals, FastAPI app) directly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kragen.plugins.context import PluginContext


PluginKind = Literal["skill", "tool", "backend", "channel", "composite"]
"""Kind advertised by a plugin.

* ``skill`` — injects prompt fragments into the worker prompt.
* ``tool`` — declares an MCP server spawned per task workspace.
* ``backend`` — mounts a FastAPI sub-router on the API process.
* ``channel`` — describes an out-of-process channel (informational only; the
  channel itself runs as a separate service and talks to the public API).
* ``composite`` — multiple of the above at once.
"""


class PluginManifest(BaseModel):
    """Static metadata declared by every plugin."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$",
        description="Globally unique plugin id (kebab or dotted).",
    )
    version: str = Field(description="Plugin version (SemVer recommended).")
    kind: PluginKind
    name: str = Field(default="", description="Human-readable name; defaults to id.")
    description: str = ""
    author: str = ""
    homepage: str | None = None
    requires: list[str] = Field(
        default_factory=list,
        description="Other plugin ids that must be enabled before this one.",
    )
    config_schema: dict[str, Any] | None = Field(
        default=None,
        description="JSON schema (draft 2020-12) for the plugin configuration block.",
    )


class SkillSpec(BaseModel):
    """Declarative prompt-level extension."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    title: str
    description: str = ""
    prompt: str = Field(
        description="System-prompt fragment appended to the worker prompt.",
    )
    priority: int = Field(
        default=100,
        description="Lower priority fragments appear first in the prompt.",
    )
    when: Literal["always", "mention", "manual"] = Field(
        default="always",
        description=(
            "'always' = attach to every task; "
            "'mention' = attach when user message contains any of `triggers`; "
            "'manual' = attach only when explicitly bound to a session."
        ),
    )
    triggers: list[str] = Field(
        default_factory=list,
        description="Case-insensitive substrings for `when='mention'`.",
    )


class MCPServerSpec(BaseModel):
    """One MCP server made available to the Cursor worker via generated mcp.json."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    command: str = Field(description="Executable to spawn (e.g. 'python', 'node').")
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    description: str = ""


class BackendSpec(BaseModel):
    """FastAPI sub-router to mount on the Kragen API process."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    router: APIRouter
    prefix: str = Field(
        default="",
        description="URL prefix; '/plugins/<id>' is the conventional default.",
    )
    tags: list[str] = Field(default_factory=list)


class ChannelSpec(BaseModel):
    """Descriptor of an external channel process (informational)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    title: str
    description: str = ""
    launch_hint: str = Field(
        default="",
        description="How an operator starts this channel (command, docker image, etc.).",
    )


HookFn = Callable[["PluginContext"], Awaitable[None]]


@runtime_checkable
class KragenPlugin(Protocol):
    """Minimal contract satisfied by every plugin instance."""

    manifest: PluginManifest

    def setup(self, ctx: "PluginContext") -> None:
        """Register specs. Called once, synchronously, during ``PluginManager`` init."""
        ...


class BasePlugin:
    """Convenience base class — most plugins subclass this instead of the protocol."""

    manifest: PluginManifest

    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest

    def setup(self, ctx: "PluginContext") -> None:  # noqa: ARG002
        """Override in subclasses to register skills / MCP / routers."""
        return None

    async def shutdown(self) -> None:
        """Optional async teardown hook (called during app shutdown)."""
        return None
