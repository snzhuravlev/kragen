"""workspace-mcp: sandboxed filesystem and git helpers (MVP stubs)."""

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kragen-workspace")

ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/tmp/kragen-workspace")).resolve()


def _under_root(p: Path) -> bool:
    try:
        p.resolve().relative_to(ROOT)
        return True
    except ValueError:
        return False


@mcp.tool()
def list_files(path: str = ".") -> str:
    """List files under path within workspace root."""
    target = (ROOT / path).resolve()
    if not _under_root(target):
        return "error: path outside workspace"
    if not target.exists():
        return "error: not found"
    names = sorted(str(p.relative_to(ROOT)) for p in target.iterdir())
    return "\n".join(names) or "(empty)"


@mcp.tool()
def read_file(path: str) -> str:
    """Read a UTF-8 text file within workspace."""
    target = (ROOT / path).resolve()
    if not _under_root(target):
        return "error: path outside workspace"
    if not target.is_file():
        return "error: not a file"
    return target.read_text(encoding="utf-8", errors="replace")


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Write file (audit in production)."""
    target = (ROOT / path).resolve()
    if not _under_root(target):
        return "error: path outside workspace"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"ok: wrote {target.relative_to(ROOT)}"


@mcp.tool()
def search_code(query: str, path: str = ".") -> str:
    """Naive substring search for MVP (replace with ripgrep)."""
    base = (ROOT / path).resolve()
    if not _under_root(base):
        return "error: path outside workspace"
    hits: list[str] = []
    for p in base.rglob("*") if base.is_dir() else [base]:
        if p.is_file() and ".git" not in p.parts:
            text = p.read_text(encoding="utf-8", errors="ignore")
            if query in text:
                hits.append(str(p.relative_to(ROOT)))
    return "\n".join(hits[:50]) or "(no hits)"


@mcp.tool()
def create_artifact(path: str, content: str) -> str:
    """Create an artifact file under workspace artifacts directory."""
    return write_file(str(Path("artifacts") / path), content)


@mcp.tool()
def get_git_status() -> str:
    """Return git status (stub if not a repo)."""
    return "[stub] git status: not implemented"


@mcp.tool()
def get_git_diff() -> str:
    """Return git diff (stub)."""
    return "[stub] git diff: not implemented"


@mcp.tool()
def run_tests(command: str = "pytest") -> str:
    """Run tests (approval may be required upstream)."""
    return f"[stub] run_tests command={command!r}"


@mcp.tool()
def run_command(command: str, approval_required: bool = True) -> str:
    """Run shell command (approval_required should be enforced by policy)."""
    return f"[stub] run_command blocked in MVP approval_required={approval_required} cmd={command!r}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
