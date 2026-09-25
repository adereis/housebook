"""Sidecar envelope: shared schema for AI-classified documents.

Every ingestion source (HSA, CC, Tax, Amazon-XLSX) produces JSON sidecars
that wrap source-specific data inside a common envelope. The envelope
records what the source file is, who classified it, and provides a
single audit boundary between raw input and the database.

Envelope shape (schema_version=1):

    {
      "schema_version": "1",
      "source": "hsa | cc | tax | amazon",
      "source_file": {
        "path": "hsa/2024/2024-02-06__Family-Dental__REC__John__60.71.pdf",
        "sha256": "<hex>",
        "size_bytes": 123456,
        "mime_type": "application/pdf"
      },
      "classified_at": "2026-05-02T10:30:00Z",
      "classified_by": "agent",
      "classifier_notes": null,
      "data": { ... source-specific block ... }
    }
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1"

VALID_SOURCES = ("hsa", "cc", "tax", "amazon")


class SidecarError(Exception):
    """Raised when a sidecar fails envelope validation."""


@dataclass
class SourceFile:
    path: str            # workspace-relative
    sha256: str
    size_bytes: int
    mime_type: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mime_type": self.mime_type,
        }


@dataclass
class Sidecar:
    source: str
    source_file: SourceFile
    data: dict
    classified_at: str
    classified_by: str = "agent"
    classifier_notes: str | None = None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "source_file": self.source_file.to_dict(),
            "classified_at": self.classified_at,
            "classified_by": self.classified_by,
            "classifier_notes": self.classifier_notes,
            "data": self.data,
        }


def sha256_file(path: str, chunk_size: int = 65536) -> str:
    """Compute SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def build_source_file(
    path: str, workspace_root: str | None = None,
) -> SourceFile:
    """Build a SourceFile descriptor for an existing file on disk.

    `path` may be absolute or workspace-relative. The resulting
    SourceFile.path is workspace-relative (POSIX separators) when
    workspace_root is provided.
    """
    abs_path = os.path.abspath(path)
    if workspace_root:
        rel = os.path.relpath(abs_path, os.path.abspath(workspace_root))
        rel = rel.replace(os.sep, "/")
    else:
        rel = path.replace(os.sep, "/")
    size = os.path.getsize(abs_path)
    mime, _ = mimetypes.guess_type(abs_path)
    return SourceFile(
        path=rel,
        sha256=sha256_file(abs_path),
        size_bytes=size,
        mime_type=mime or "application/octet-stream",
    )


def utcnow_iso() -> str:
    """Current UTC time, ISO-8601 with 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_envelope(d: Any) -> None:
    """Raise SidecarError if `d` is not a valid envelope.

    Validates structure only; source-specific `data` blocks are
    validated by each module's schema.
    """
    if not isinstance(d, dict):
        raise SidecarError("sidecar must be a JSON object")

    sv = d.get("schema_version")
    if sv != SCHEMA_VERSION:
        raise SidecarError(
            f"unsupported schema_version {sv!r}; expected {SCHEMA_VERSION!r}"
        )

    source = d.get("source")
    if source not in VALID_SOURCES:
        raise SidecarError(
            f"invalid source {source!r}; expected one of {VALID_SOURCES}"
        )

    sf = d.get("source_file")
    if not isinstance(sf, dict):
        raise SidecarError("source_file must be an object")
    for key in ("path", "sha256", "size_bytes", "mime_type"):
        if key not in sf:
            raise SidecarError(f"source_file.{key} is required")
    if not isinstance(sf["path"], str) or not sf["path"]:
        raise SidecarError("source_file.path must be a non-empty string")
    if not isinstance(sf["sha256"], str) or len(sf["sha256"]) != 64:
        raise SidecarError(
            "source_file.sha256 must be a 64-character hex string"
        )
    if not isinstance(sf["size_bytes"], int) or sf["size_bytes"] < 0:
        raise SidecarError(
            "source_file.size_bytes must be a non-negative integer"
        )

    if "classified_at" not in d:
        raise SidecarError("classified_at is required")
    if "data" not in d or not isinstance(d["data"], dict):
        raise SidecarError("data must be an object")


def load(path: str) -> Sidecar:
    """Read and validate a sidecar JSON file."""
    with open(path) as f:
        d = json.load(f)
    validate_envelope(d)
    sf = d["source_file"]
    return Sidecar(
        schema_version=d["schema_version"],
        source=d["source"],
        source_file=SourceFile(
            path=sf["path"],
            sha256=sf["sha256"],
            size_bytes=sf["size_bytes"],
            mime_type=sf["mime_type"],
        ),
        classified_at=d["classified_at"],
        classified_by=d.get("classified_by", "agent"),
        classifier_notes=d.get("classifier_notes"),
        data=d["data"],
    )


def dump(sidecar: Sidecar, path: str) -> None:
    """Write a sidecar to disk in the canonical format."""
    with open(path, "w") as f:
        json.dump(sidecar.to_dict(), f, indent=2, sort_keys=False)
        f.write("\n")
