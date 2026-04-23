"""db-mcp: readonly SQL templates only (no ad-hoc SQL in MVP)."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kragen-db")

# Whitelisted templates: id -> SQL with %(param)s placeholders
TEMPLATES: dict[str, str] = {
    "count_users": "SELECT COUNT(*) AS c FROM users",
    "sessions_by_workspace": (
        "SELECT id, title, created_at FROM sessions WHERE workspace_id = %(workspace_id)s ORDER BY created_at DESC LIMIT 50"
    ),
}


@mcp.tool()
def list_available_queries() -> str:
    """List whitelisted template ids."""
    return "\n".join(sorted(TEMPLATES))


@mcp.tool()
def describe_dataset(name: str) -> str:
    """Describe a named dataset view (stub)."""
    return f"[stub] describe_dataset name={name!r}"


@mcp.tool()
def run_readonly_query(query_template_id: str, params_json: str = "{}") -> str:
    """
    Execute a whitelisted readonly template.

    params_json must be a JSON object matching template parameters.
    """
    if query_template_id not in TEMPLATES:
        return "error: unknown template"
    return f"[stub] would run template={query_template_id} params={params_json}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
