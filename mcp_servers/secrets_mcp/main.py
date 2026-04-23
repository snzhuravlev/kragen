"""secrets-mcp: bridge to Vault / env (MVP returns masked placeholders only)."""

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kragen-secrets")


def _mask(name: str) -> str:
    return f"<<secret:{name}>>"


@mcp.tool()
def get_secret(name: str) -> str:
    """Fetch secret material (never log raw values)."""
    if os.environ.get("SECRETS_MCP_DRY_RUN", "1") == "1":
        return _mask(name)
    return _mask(name)


@mcp.tool()
def get_db_credentials(service_name: str) -> str:
    """Return short-lived DB credentials for a named service (stub)."""
    return _mask(f"db:{service_name}")


@mcp.tool()
def get_api_token(service_name: str) -> str:
    """Return API token for external integration (stub)."""
    return _mask(f"token:{service_name}")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
