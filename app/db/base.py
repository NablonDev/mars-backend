"""SQLAlchemy declarative base, metadata, and shared database schema configuration.

Re-exports mars_common's db/base module. mars-common is the single source of
truth for ORM model metadata, shared with mars-bff — every model in this
codebase (via app/models/) and every model mars-bff imports registers against
the exact same Base/metadata object defined there, not a local copy. This
file's own historical definitions were byte-identical to mars-common's before
this change; nothing here behaves differently after it.
"""

from mars_common.db.base import (
    CMIR_SCHEMA,
    INDEX_NAMING_CONVENTION,
    JSONB_OR_JSON,
    LANGGRAPH_SCHEMA,
    PENALTIES_SCHEMA,
    PROCESS_SCHEMA,
    UUID_PK,
    Base,
    TimestampMixin,
    generate_uuid7,
)

__all__ = [
    "CMIR_SCHEMA",
    "INDEX_NAMING_CONVENTION",
    "JSONB_OR_JSON",
    "LANGGRAPH_SCHEMA",
    "PENALTIES_SCHEMA",
    "PROCESS_SCHEMA",
    "UUID_PK",
    "Base",
    "TimestampMixin",
    "generate_uuid7",
]
