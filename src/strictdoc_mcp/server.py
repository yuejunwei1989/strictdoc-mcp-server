"""strictdoc-mcp: a read-only MCP server over a StrictDoc JSON export.

Exposes requirements, traceability and consistency checks as MCP tools so
that AI agents can read the requirement graph without touching the baseline.

Usage:
    strictdoc-mcp /path/to/output/json/index.json
    STRICTDOC_MCP_JSON=/path/to/index.json strictdoc-mcp

Generate the export first:
    strictdoc export docs --formats=json --output-dir output
"""

from __future__ import annotations

import json
import sys

try:
    # mcp 1.x
    from mcp.server.fastmcp import FastMCP
except ImportError:  # mcp >= 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore[no-redef]

from strictdoc_mcp.index import ReloadableIndex, resolve_json_path

mcp = FastMCP(
    "strictdoc",
    instructions=(
        "Read-only access to a StrictDoc requirements repository (JSON export). "
        "All tool responses carry a 'source' reference (document UID + node UID) "
        "that must be preserved when citing requirements. This server cannot "
        "modify documents; any change must go through review/PR on the .sdoc files."
    ),
)

_index: ReloadableIndex | None = None


def _idx() -> ReloadableIndex:
    global _index
    if _index is None:
        _index = ReloadableIndex(resolve_json_path())
    return _index


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------- tools


@mcp.tool()
def strictdoc_info() -> str:
    """Dataset overview: documents, requirement counts by status, relation counts and unresolved (dangling) relations."""
    return _dump(_idx().get().summary())


@mcp.tool()
def list_documents() -> str:
    """List all documents with their UID, title and requirement IDs."""
    return _dump(_idx().get().list_documents())


@mcp.tool()
def get_document_tree(doc_uid: str) -> str:
    """Show the full section/requirement hierarchy of one document (by document UID, e.g. 'SWRS')."""
    try:
        return _dump(_idx().get().document_tree(doc_uid))
    except KeyError as e:
        return _dump({"error": str(e)})


@mcp.tool()
def get_requirement(uid: str) -> str:
    """Get one requirement by UID with all fields, section breadcrumb, resolved outgoing/incoming relations and its source reference."""
    index = _idx().get()
    if uid not in index.nodes:
        return _dump({"error": f"UID not found: {uid}", "hint": "use search_requirements or list_documents"})
    return _dump(index.get_requirement(uid))


@mcp.tool()
def get_traceability(uid: str, max_depth: int = 5) -> str:
    """Full upstream and downstream traceability chains from a requirement UID. Every chain step carries a source reference."""
    index = _idx().get()
    if uid not in index.nodes:
        return _dump({"error": f"UID not found: {uid}"})
    return _dump(index.traceability(uid, max_depth=max(1, min(int(max_depth), 10))))


@mcp.tool()
def search_requirements(
    query: str = "",
    status: str = "",
    doc_uid: str = "",
    limit: int = 20,
) -> str:
    """Search requirements by free-text substring (UID/title/statement/rationale/tags). Optional filters: exact status (e.g. 'Draft'), document UID. Returns compact hits with source references."""
    return _dump(
        _idx().get().search(
            query=query,
            status=status or None,
            doc_uid=doc_uid or None,
            limit=max(1, min(int(limit), 100)),
        )
    )


@mcp.tool()
def find_orphans() -> str:
    """Consistency check: requirements with no upstream Parent link (orphan requirements) and requirements no downstream item points to (unimplemented). Useful gap analysis before an assessment."""
    return _dump(_idx().get().find_orphans())


def main() -> None:
    # Fail fast with a clear message before the MCP handshake.
    _idx()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
