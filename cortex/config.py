"""Cortex configuration — everything from environment variables (12-factor)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Config:
    # storage
    database_url: str = field(default_factory=lambda: os.environ.get(
        "DATABASE_URL", "postgres://cortex:cortex@localhost:5432/cortex"))

    # identity / security
    admin_key: str = field(default_factory=lambda: os.environ.get("CORTEX_ADMIN_KEY", ""))
    hook_token: str = field(default_factory=lambda: os.environ.get("CORTEX_HOOK_TOKEN", ""))
    lan_cidrs: list[str] = field(default_factory=lambda: [
        c.strip() for c in os.environ.get("CORTEX_LAN_CIDRS", "").split(",") if c.strip()
    ])

    # service
    host: str = field(default_factory=lambda: os.environ.get("CORTEX_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("CORTEX_PORT", 8738))
    log_level: str = field(default_factory=lambda: os.environ.get("CORTEX_LOG_LEVEL", "INFO"))

    # LLM extraction — disabled until configured (NFR-3)
    llm_base_url: str = field(default_factory=lambda: os.environ.get("CORTEX_LLM_BASE_URL", "").rstrip("/"))
    llm_api_key: str = field(default_factory=lambda: os.environ.get("CORTEX_LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.environ.get("CORTEX_LLM_MODEL", "deepseek-chat"))
    # request response_format json_object; some providers/models (OpenRouter
    # free tiers notably) reject it — set CORTEX_LLM_JSON_MODE=false to
    # disable (extraction then relies on defensive JSON parsing)
    llm_json_mode: bool = field(default_factory=lambda: _bool("CORTEX_LLM_JSON_MODE", True))

    # embeddings — local ONNX (fastembed)
    embed_model: str = field(default_factory=lambda: os.environ.get("CORTEX_EMBED_MODEL", "BAAI/bge-m3"))
    embed_dim: int = field(default_factory=lambda: _int("CORTEX_EMBED_DIM", 1024))

    # rerank (P1, off by default)
    rerank_enabled: bool = field(default_factory=lambda: _bool("CORTEX_RERANK_ENABLED"))
    rerank_model: str = field(default_factory=lambda: os.environ.get(
        "CORTEX_RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2"))

    # capture (FR-11)
    capture_timeout: int = field(default_factory=lambda: _int("CORTEX_CAPTURE_TIMEOUT", 15))
    capture_max_bytes: int = field(default_factory=lambda: _int("CORTEX_CAPTURE_MAX_BYTES", 5 * 1024 * 1024))

    # file uploads: content-addressed blob store shared by every harness
    files_dir: str = field(default_factory=lambda: os.environ.get("CORTEX_FILES_DIR", "/data/files"))
    upload_max_bytes: int = field(default_factory=lambda: _int("CORTEX_UPLOAD_MAX_BYTES", 20 * 1024 * 1024))

    # digest / context sizing (FR-10: brain_context ≤ 25 KB)
    context_max_bytes: int = field(default_factory=lambda: _int("CORTEX_CONTEXT_MAX_BYTES", 25 * 1024))

    # librarian
    librarian_batch: int = field(default_factory=lambda: _int("CORTEX_LIBRARIAN_BATCH", 10))
    librarian_poll_seconds: float = field(default_factory=lambda: float(os.environ.get(
        "CORTEX_LIBRARIAN_POLL_SECONDS", "2.0")))

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url)


def get_config() -> Config:
    return Config()
