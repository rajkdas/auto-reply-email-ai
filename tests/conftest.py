"""Shared pytest config: project root on sys.path, async mode, fixtures."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Make sure ``import replydesk`` resolves to the inner package.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Disable network/provider calls by default.
os.environ.setdefault("MODE", "dry_run")
os.environ.setdefault("LLM_PROVIDER", "openai")
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-real")
os.environ.setdefault("IMAP_USER", "support@example.com")


@pytest_asyncio.fixture
async def tmp_db(tmp_path):
    from replydesk.store import Store
    s = Store(str(tmp_path / "test.db"))
    await s.connect()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def settings(tmp_path):
    from replydesk.config import Settings
    return Settings(
        mode="dry_run",
        llm_provider="openai",
        openai_api_key="test",
        imap_user="support@example.com",
        smtp_user="support@example.com",
        db_path=str(tmp_path / "test.db"),
        products_file=str(Path(__file__).parent.parent / "products.example.yaml"),
    )
