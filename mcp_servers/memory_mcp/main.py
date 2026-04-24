"""memory-mcp: long-term memory access backed by PostgreSQL."""

from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import psycopg
from mcp.server.fastmcp import FastMCP
from psycopg.rows import dict_row

mcp = FastMCP("kragen-memory")


def _normalize_dsn(raw: str) -> str:
    """Convert SQLAlchemy-style DSNs to psycopg-compatible DSNs."""
    dsn = raw.strip()
    dsn = dsn.replace("+asyncpg", "")
    dsn = dsn.replace("+psycopg", "")
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn.removeprefix("postgres://")
    return dsn


def _database_url() -> str:
    """
    Resolve the PostgreSQL URL for memory-mcp.

    Priority:
    1) MEMORY_MCP_DATABASE_URL
    2) KRAGEN_DATABASE__URL
    3) DATABASE_URL
    """
    raw = (
        os.environ.get("MEMORY_MCP_DATABASE_URL")
        or os.environ.get("KRAGEN_DATABASE__URL")
        or os.environ.get("DATABASE_URL")
    )
    if not raw:
        raise RuntimeError(
            "memory-mcp requires MEMORY_MCP_DATABASE_URL or KRAGEN_DATABASE__URL in env."
        )
    return _normalize_dsn(raw)


def _default_workspace_id() -> uuid.UUID | None:
    """Optional default workspace id for tool calls that omit it."""
    value = os.environ.get("MEMORY_MCP_WORKSPACE_ID") or os.environ.get("KRAGEN_DEFAULT_WORKSPACE_ID")
    if not value:
        return None
    return uuid.UUID(value)


@contextmanager
def _conn() -> Any:
    """Yield a psycopg connection with dict row output."""
    conn = psycopg.connect(_database_url(), row_factory=dict_row, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def _serialize(payload: dict[str, Any]) -> str:
    """Return compact JSON for MCP tool responses."""
    return json.dumps(payload, ensure_ascii=True, default=str)


def _parse_uuid(value: str, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid UUID, got: {value!r}") from exc


def _score_text(query: str, text: str) -> float:
    """Fallback lexical score for mixed result sets."""
    tokens = [t for t in re.split(r"\s+", query.lower().strip()) if t]
    if not tokens:
        return 0.0
    text_norm = text.lower()
    hits = sum(1 for t in tokens if t in text_norm)
    return float(hits) / float(len(tokens))


@mcp.tool()
def search_memory(
    query: str,
    scope: str = "workspace",
    filters: dict[str, Any] | None = None,
    k: int = 8,
) -> str:
    """
    Search long-term memory using lexical hybrid retrieval.

    Supported filters:
    - workspace_id (uuid)
    - session_id (uuid, used for scope='session')
    """
    if k < 1:
        return _serialize({"ok": False, "error": "k must be >= 1"})
    filters = filters or {}
    workspace_id_raw = filters.get("workspace_id")
    session_id_raw = filters.get("session_id")
    workspace_id = _parse_uuid(str(workspace_id_raw), "workspace_id") if workspace_id_raw else _default_workspace_id()
    session_id = _parse_uuid(str(session_id_raw), "session_id") if session_id_raw else None
    if scope == "session" and not session_id:
        return _serialize({"ok": False, "error": "scope=session requires filters.session_id"})

    with _conn() as conn, conn.cursor() as cur:
        if scope == "session":
            cur.execute(
                "SELECT workspace_id FROM sessions WHERE id = %s",
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                return _serialize({"ok": False, "error": f"session not found: {session_id}"})
            workspace_id = row["workspace_id"]
        if not workspace_id:
            return _serialize(
                {
                    "ok": False,
                    "error": "workspace_id is required (set filters.workspace_id or MEMORY_MCP_WORKSPACE_ID).",
                }
            )

        # Document chunks
        cur.execute(
            """
            SELECT
                dc.id::text AS id,
                'chunk' AS memory_type,
                d.id::text AS document_id,
                COALESCE(d.title, d.source_ref, 'document') AS source_ref,
                LEFT(dc.content, 1400) AS content
            FROM document_chunks dc
            JOIN documents d ON d.id = dc.document_id
            WHERE d.workspace_id = %s
              AND (
                    dc.content ILIKE concat('%%', CAST(%s AS text), '%%')
                 OR to_tsvector('simple', dc.content) @@ plainto_tsquery('simple', CAST(%s AS text))
              )
            ORDER BY dc.created_at DESC
            LIMIT %s
            """,
            (workspace_id, query, query, max(k * 3, 16)),
        )
        chunk_rows = cur.fetchall()

        # Session summaries
        if session_id:
            cur.execute(
                """
                SELECT
                    ss.id::text AS id,
                    'session_summary' AS memory_type,
                    NULL::text AS document_id,
                    s.id::text AS source_ref,
                    LEFT(ss.summary_text, 1400) AS content
                FROM session_summaries ss
                JOIN sessions s ON s.id = ss.session_id
                WHERE ss.session_id = %s
                  AND (
                        ss.summary_text ILIKE concat('%%', CAST(%s AS text), '%%')
                     OR to_tsvector('simple', ss.summary_text) @@ plainto_tsquery('simple', CAST(%s AS text))
                  )
                ORDER BY ss.updated_at DESC
                LIMIT %s
                """,
                (session_id, query, query, max(k, 4)),
            )
        else:
            cur.execute(
                """
                SELECT
                    ss.id::text AS id,
                    'session_summary' AS memory_type,
                    NULL::text AS document_id,
                    s.id::text AS source_ref,
                    LEFT(ss.summary_text, 1400) AS content
                FROM session_summaries ss
                JOIN sessions s ON s.id = ss.session_id
                WHERE s.workspace_id = %s
                  AND (
                        ss.summary_text ILIKE concat('%%', CAST(%s AS text), '%%')
                     OR to_tsvector('simple', ss.summary_text) @@ plainto_tsquery('simple', CAST(%s AS text))
                  )
                ORDER BY ss.updated_at DESC
                LIMIT %s
                """,
                (workspace_id, query, query, max(k, 4)),
            )
        summary_rows = cur.fetchall()

        # Semantic facts
        cur.execute(
            """
            SELECT
                sf.id::text AS id,
                'semantic_fact' AS memory_type,
                NULL::text AS document_id,
                COALESCE(sf.source_ref, sf.entity) AS source_ref,
                LEFT(sf.fact_text, 1400) AS content
            FROM semantic_facts sf
            WHERE sf.workspace_id = %s
              AND (
                    sf.fact_text ILIKE concat('%%', CAST(%s AS text), '%%')
                 OR sf.entity ILIKE concat('%%', CAST(%s AS text), '%%')
                 OR to_tsvector('simple', sf.fact_text) @@ plainto_tsquery('simple', CAST(%s AS text))
              )
            ORDER BY sf.updated_at DESC
            LIMIT %s
            """,
            (workspace_id, query, query, query, max(k * 2, 8)),
        )
        fact_rows = cur.fetchall()

    merged = [*chunk_rows, *summary_rows, *fact_rows]
    for row in merged:
        row["score"] = _score_text(query, row.get("content", ""))
    merged.sort(key=lambda r: (r["score"], r["memory_type"] == "chunk"), reverse=True)
    top = merged[:k]
    return _serialize(
        {
            "ok": True,
            "workspace_id": str(workspace_id),
            "scope": scope,
            "k": k,
            "results": top,
        }
    )


@mcp.tool()
def get_document(doc_id: str) -> str:
    """Return document metadata and a preview of its chunks."""
    document_id = _parse_uuid(doc_id, "doc_id")
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                id::text AS id,
                workspace_id::text AS workspace_id,
                user_id::text AS user_id,
                source_type,
                source_ref,
                title,
                mime_type,
                metadata,
                content_hash,
                visibility,
                created_at,
                updated_at
            FROM documents
            WHERE id = %s
            """,
            (document_id,),
        )
        document = cur.fetchone()
        if not document:
            return _serialize({"ok": False, "error": f"document not found: {doc_id}"})

        cur.execute(
            """
            SELECT
                id::text AS id,
                chunk_index,
                LEFT(content, 500) AS content_preview,
                content_hash,
                created_at
            FROM document_chunks
            WHERE document_id = %s
            ORDER BY chunk_index
            LIMIT 30
            """,
            (document_id,),
        )
        chunks = cur.fetchall()
    return _serialize({"ok": True, "document": document, "chunks": chunks})


@mcp.tool()
def get_chunk(chunk_id: str) -> str:
    """Return full chunk content with document provenance."""
    cid = _parse_uuid(chunk_id, "chunk_id")
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                dc.id::text AS id,
                dc.chunk_index,
                dc.content,
                dc.metadata,
                dc.content_hash,
                dc.created_at,
                d.id::text AS document_id,
                d.workspace_id::text AS workspace_id,
                d.title,
                d.source_ref,
                d.source_type
            FROM document_chunks dc
            JOIN documents d ON d.id = dc.document_id
            WHERE dc.id = %s
            """,
            (cid,),
        )
        chunk = cur.fetchone()
    if not chunk:
        return _serialize({"ok": False, "error": f"chunk not found: {chunk_id}"})
    return _serialize({"ok": True, "chunk": chunk})


@mcp.tool()
def get_related_context(session_id: str, query: str, k: int = 8) -> str:
    """Return compact context from session summaries, recent messages, and memory search."""
    sid = _parse_uuid(session_id, "session_id")
    if k < 1:
        return _serialize({"ok": False, "error": "k must be >= 1"})

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id::text, workspace_id::text FROM sessions WHERE id = %s", (sid,))
        session = cur.fetchone()
        if not session:
            return _serialize({"ok": False, "error": f"session not found: {session_id}"})

        cur.execute(
            """
            SELECT id::text AS id, summary_text, updated_at
            FROM session_summaries
            WHERE session_id = %s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (sid,),
        )
        summary = cur.fetchone()

        cur.execute(
            """
            SELECT id::text AS id, role, LEFT(content, 700) AS content, created_at
            FROM messages
            WHERE session_id = %s
            ORDER BY created_at DESC
            LIMIT 12
            """,
            (sid,),
        )
        messages = cur.fetchall()

    search_payload = json.loads(
        search_memory(
            query=query,
            scope="session",
            filters={"session_id": session_id, "workspace_id": session["workspace_id"]},
            k=k,
        )
    )
    return _serialize(
        {
            "ok": True,
            "session": session,
            "summary": summary,
            "recent_messages": list(reversed(messages)),
            "memory_hits": search_payload.get("results", []),
        }
    )


@mcp.tool()
def save_session_summary(session_id: str, summary: str) -> str:
    """Insert or update the latest summary row for a session."""
    sid = _parse_uuid(session_id, "session_id")
    if not summary.strip():
        return _serialize({"ok": False, "error": "summary must not be empty"})
    now = datetime.now(timezone.utc)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id::text FROM sessions WHERE id = %s", (sid,))
        if not cur.fetchone():
            return _serialize({"ok": False, "error": f"session not found: {session_id}"})

        cur.execute(
            """
            SELECT id
            FROM session_summaries
            WHERE session_id = %s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (sid,),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                """
                UPDATE session_summaries
                SET summary_text = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (summary, now, existing["id"]),
            )
            summary_id = str(existing["id"])
            action = "updated"
        else:
            cur.execute(
                """
                INSERT INTO session_summaries (session_id, summary_text)
                VALUES (%s, %s)
                RETURNING id::text
                """,
                (sid, summary),
            )
            summary_id = cur.fetchone()["id"]
            action = "created"
    return _serialize(
        {
            "ok": True,
            "action": action,
            "session_id": session_id,
            "summary_id": summary_id,
            "summary_chars": len(summary),
        }
    )


@mcp.tool()
def upsert_semantic_fact(
    entity: str,
    fact: str,
    confidence: float = 1.0,
    source_ref: str | None = None,
    workspace_id: str | None = None,
) -> str:
    """Upsert a durable semantic fact in workspace scope."""
    if not entity.strip() or not fact.strip():
        return _serialize({"ok": False, "error": "entity and fact must not be empty"})
    if confidence < 0 or confidence > 1:
        return _serialize({"ok": False, "error": "confidence must be between 0 and 1"})
    ws = _parse_uuid(workspace_id, "workspace_id") if workspace_id else _default_workspace_id()
    if not ws:
        return _serialize(
            {
                "ok": False,
                "error": "workspace_id is required (argument or MEMORY_MCP_WORKSPACE_ID).",
            }
        )
    now = datetime.now(timezone.utc)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, confidence
            FROM semantic_facts
            WHERE workspace_id = %s
              AND entity = %s
              AND fact_text = %s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (ws, entity.strip(), fact.strip()),
        )
        existing = cur.fetchone()
        if existing:
            new_confidence = max(float(existing["confidence"]), confidence)
            cur.execute(
                """
                UPDATE semantic_facts
                SET confidence = %s,
                    source_ref = COALESCE(%s, source_ref),
                    updated_at = %s
                WHERE id = %s
                """,
                (new_confidence, source_ref, now, existing["id"]),
            )
            fact_id = str(existing["id"])
            action = "updated"
            effective_confidence = new_confidence
        else:
            cur.execute(
                """
                INSERT INTO semantic_facts (workspace_id, entity, fact_text, confidence, source_ref)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id::text
                """,
                (ws, entity.strip(), fact.strip(), confidence, source_ref),
            )
            fact_id = cur.fetchone()["id"]
            action = "created"
            effective_confidence = confidence

    return _serialize(
        {
            "ok": True,
            "action": action,
            "workspace_id": str(ws),
            "fact_id": fact_id,
            "confidence": effective_confidence,
        }
    )


def _link_memory(memory_id: str, link_type: str) -> str:
    mid = _parse_uuid(memory_id, "memory_id")
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO memory_links (from_id, to_id, link_type, metadata)
            VALUES (%s, %s, %s, %s)
            RETURNING id::text
            """,
            (
                mid,
                mid,
                link_type,
                json.dumps({"source": "memory-mcp"}, ensure_ascii=True),
            ),
        )
        link_id = cur.fetchone()["id"]
    return _serialize(
        {
            "ok": True,
            "memory_id": memory_id,
            "link_type": link_type,
            "memory_link_id": link_id,
        }
    )


@mcp.tool()
def pin_memory(memory_id: str) -> str:
    """Pin memory by creating a self-referential memory_links record."""
    return _link_memory(memory_id, "pinned")


@mcp.tool()
def forget_memory(memory_id: str) -> str:
    """Mark memory as forgotten via memory_links and metadata tombstone."""
    mid = _parse_uuid(memory_id, "memory_id")
    with _conn() as conn, conn.cursor() as cur:
        # Best effort: set metadata.tombstone on known memory tables.
        for table in ("documents", "document_chunks", "semantic_facts"):
            cur.execute(
                f"""
                UPDATE {table}
                SET metadata = COALESCE(metadata, '{{}}'::jsonb)
                             || %s::jsonb
                WHERE id = %s
                """,
                (json.dumps({"forgotten": True, "forgotten_by": "memory-mcp"}), mid),
            )
    return _link_memory(memory_id, "forgotten")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
