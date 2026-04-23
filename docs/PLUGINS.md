# Kragen plugins

External modules that extend Kragen's functionality. Four kinds of plugins are
supported, all discovered through the same Python entry-point group.

## Plugin kinds

| Kind       | What it adds                                         | Runs where                 |
| ---------- | ---------------------------------------------------- | -------------------------- |
| `skill`    | Prompt fragment injected into the Cursor worker      | In the worker prompt       |
| `tool`     | An MCP server spawned per task workspace             | Subprocess of the worker   |
| `backend`  | FastAPI sub-router mounted on the API process        | Kragen API process         |
| `channel`  | Descriptor of an external channel process (advisory) | External process           |
| `composite`| Any combination of the above                         | As declared by each spec   |

> Trust model: plugins are regular Python code running in the Kragen process
> (or, for `tool` plugins, in a subprocess spawned by Cursor). There is no
> sandboxing. Only install plugins you trust — same posture as VS Code or
> Cursor extensions.

## Quick start: ship a plugin from your own package

Create a Python package and expose a plugin factory via the `kragen.plugins`
entry-point group.

```toml
# my_plugin/pyproject.toml
[project]
name = "kragen-plugin-jira"
version = "0.1.0"

[project.entry-points."kragen.plugins"]
kragen-plugin-jira = "kragen_plugin_jira:plugin"
```

```python
# my_plugin/kragen_plugin_jira/__init__.py
from kragen.plugins import BasePlugin, PluginContext, PluginManifest, SkillSpec


class JiraPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(
            PluginManifest(
                id="kragen-plugin-jira",
                version="0.1.0",
                kind="skill",
                name="Jira helper",
            )
        )

    def setup(self, ctx: PluginContext) -> None:
        base_url = ctx.config.get("base_url", "https://jira.example.com")
        ctx.register_skill(
            SkillSpec(
                id="jira-triage",
                title="Jira triage helper",
                prompt=(
                    "When the user mentions a Jira ticket (e.g. ABC-123), "
                    f"assume the base URL is {base_url} and include a link "
                    "to the ticket in your answer."
                ),
                when="mention",
                triggers=["jira", "ticket"],
            )
        )


def plugin() -> JiraPlugin:
    return JiraPlugin()
```

Install it into the same environment as Kragen:

```bash
pip install ./kragen-plugin-jira
```

Then enable the plugin in `configs/kragen.yaml`:

```yaml
plugins:
  enabled:
    - id: kragen-plugin-jira
      config:
        base_url: https://jira.acme.com
```

Restart the API (or call `POST /admin/plugins/kragen-plugin-jira/enable` if
already running).

## How each kind is wired

### `skill`

On every task, the orchestrator calls `PluginManager.compose_prompt(...)` which
appends all active skill fragments (sorted by `priority`) to the base prompt.
A skill's `when` field controls applicability:

- `always`   — attached to every task.
- `mention`  — attached only when the user message contains any substring in
               `triggers` (case-insensitive).
- `manual`   — reserved for the upcoming `/sessions/{id}/skills` binding API.

### `tool` (MCP server)

Before spawning `cursor agent`, the orchestrator writes
`.cursor/mcp.json` into the per-task workspace (`~/.kragen/workspaces/<id>/`)
with a union of every active `MCPServerSpec`. Cursor CLI picks the file up
automatically.

Minimum spec:

```python
ctx.register_mcp_server(MCPServerSpec(
    id="kragen-workspace",
    command="python",
    args=["-m", "my_plugin.workspace_mcp"],
    env={"WORKSPACE_ROOT": "."},
))
```

### `backend`

A plugin can mount its own FastAPI router:

```python
from fastapi import APIRouter

router = APIRouter()

@router.get("/ping")
async def ping() -> dict[str, str]:
    return {"status": "ok"}

class MyBackend(BasePlugin):
    ...
    def setup(self, ctx: PluginContext) -> None:
        ctx.include_router(router)  # mounts at /plugins/<plugin_id>
```

Backend routers are mounted once during API process startup. Toggling
`enabled` at runtime does **not** unmount or remount the router — a restart
is required for backend changes.

### `channel`

`channel` specs are informational only: they describe how an operator should
start an external channel process that talks to the public Kragen REST API.
No in-process side effects.

## Admin API

All under `/admin/plugins/*`, authenticated the same way as other
`/admin/*` routes.

| Method | Path                               | Purpose                              |
| ------ | ---------------------------------- | ------------------------------------ |
| `GET`  | `/admin/plugins`                   | List discovered plugins and specs    |
| `GET`  | `/admin/plugins/{id}`              | Descriptor for one plugin            |
| `POST` | `/admin/plugins/{id}/enable`       | Enable + run `setup()`               |
| `POST` | `/admin/plugins/{id}/disable`      | Drop skill/MCP specs                 |
| `PUT`  | `/admin/plugins/{id}/config`       | Replace config dict + re-run setup   |

## Configuration reference

```yaml
plugins:
  autoload_entry_points: true   # scan 'kragen.plugins' entry points on boot
  enabled:
    - id: kragen-plugin-jira    # only listed plugins are activated
      config:
        base_url: https://jira.acme.com
```

Discovery is always opt-in by allow-list: plugins installed in the env but not
listed under `plugins.enabled` are visible to admins (`GET /admin/plugins`)
but do nothing until enabled.

## Built-in example

Kragen ships one example plugin, `kragen-skill-concise` (see
`src/kragen/plugins/builtin/concise_skill.py`). It demonstrates the full
contract with a single always-on skill and zero configuration. Disable it via
`/admin/plugins/kragen-skill-concise/disable` or by removing it from
`plugins.enabled`.
