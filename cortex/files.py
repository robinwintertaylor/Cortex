"""File uploads: content-addressed blob store + best-effort text extraction.

Design mirrors capture.py's shape (fetch → extract → store as a searchable
note) but for bytes a harness hands us directly instead of a URL. Every
harness gets this for free the moment it's an MCP tool (NFR-8) — no
per-harness wiring needed beyond what deploy/* already does.

Blobs live on disk, content-addressed by sha256 so re-uploading identical
bytes (even from a different agent/project) never duplicates storage.
Postgres (the `notes` table) holds metadata + extracted text; only the
extracted text is searched/embedded, never the raw bytes.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .config import get_config
from .log import get_logger

log = get_logger(__name__)

# extension → extractor. Unrecognized types still get stored (downloadable)
# but with empty extracted text — never blocks the upload.
_TEXT_EXTS = (".txt", ".md", ".markdown", ".csv", ".json", ".log")


def blob_path(files_dir: str, sha256: str) -> Path:
    """Content-addressed path: files/<aa>/<bb>/<sha256><nothing>. The 2-level
    fan-out keeps any single directory from accumulating too many entries."""
    return Path(files_dir) / sha256[:2] / sha256[2:4] / sha256


def save_blob(content: bytes) -> tuple[str, str, int]:
    """Write content-addressed; no-ops if the same bytes are already stored.
    Returns (sha256_hex, storage_path, size_bytes)."""
    cfg = get_config()
    sha256 = hashlib.sha256(content).hexdigest()
    path = blob_path(cfg.files_dir, sha256)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, path)  # atomic within the same filesystem
    return sha256, str(path), len(content)


def read_blob(storage_path: str) -> bytes:
    return Path(storage_path).read_bytes()


def extract_text(filename: str, mime_type: str, content: bytes) -> str:
    """Best-effort text extraction. Never raises — returns '' for types we
    don't know how to read yet (still stored + downloadable, just not
    searchable by content)."""
    ext = Path(filename).suffix.lower()
    try:
        if ext in _TEXT_EXTS or (mime_type or "").startswith("text/"):
            return content.decode("utf-8", errors="replace")
        if ext == ".pdf" or mime_type == "application/pdf":
            return _extract_pdf(content)
        if ext == ".docx":
            return _extract_docx(content)
    except Exception:
        log.exception("text extraction failed for %s (%s)", filename, mime_type)
    return ""


def _extract_pdf(content: bytes) -> str:
    from io import BytesIO

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(content))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()


def _extract_docx(content: bytes) -> str:
    from io import BytesIO

    from docx import Document

    doc = Document(BytesIO(content))
    return "\n".join(p.text for p in doc.paragraphs).strip()
