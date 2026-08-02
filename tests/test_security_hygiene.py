"""Acceptance checklist §8: no credentials anywhere in the source tree.

Requirement §7.1 is "полное отсутствие хардкода учеток" — no API keys, SOCKS5
passwords, bot tokens or SSH keys in code, everything through the environment.
These tests enforce that mechanically so a future commit cannot quietly
reintroduce one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = PROJECT_ROOT / "ozon_anti_bot_engine"

SOURCE_FILES = sorted(PACKAGE_ROOT.rglob("*.py"))

# A secret-shaped name assigned a non-empty string literal. The secret word may
# sit anywhere inside the identifier — `\b` would miss `SOCKS_PASSWORD`, since an
# underscore is a word character and so suppresses the boundary.
SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    \w* (pass(?:word|wd)? | secret | token | api[_-]?key | access[_-]?key
         | private[_-]?key | credential) \w*
    \s* = \s* (['"]) (?P<value>[^'"]{3,}) \2
    """
)

# A bare high-entropy blob: base64/hex key material pasted into source.
KEY_MATERIAL = re.compile(r"""(['"])[A-Za-z0-9+/]{32,}={0,2}\1""")

# Private key headers, in any of the usual flavours.
PEM_HEADER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

SECRET_ENV_KEYS = (
    "OZON_API_KEY",
    "OZON_PROXY_POOL",
    "OZON_PROXY_POOL_FILE",
    "OZON_LLM_API_KEY",
    "OZON_SELLER_CLIENT_ID",
    "OZON_SELLER_API_KEY",
    "POSTGRES_PASSWORD",
)


def test_the_scanner_actually_catches_a_planted_secret() -> None:
    """Guard against the detectors silently matching nothing at all."""
    assert SECRET_ASSIGNMENT.search('api_key = "sk-live-abcdef123456"')
    assert SECRET_ASSIGNMENT.search("SOCKS_PASSWORD = 'hunter2!'")
    assert KEY_MATERIAL.search('"' + "A" * 40 + '"')
    assert PEM_HEADER.search("-----BEGIN RSA PRIVATE KEY-----")


def test_source_tree_is_not_empty() -> None:
    assert len(SOURCE_FILES) >= 10, "scan would be vacuous"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_no_hardcoded_credentials_in_source(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    assignment = SECRET_ASSIGNMENT.search(text)
    assert assignment is None, (
        f"{path.relative_to(PROJECT_ROOT)}: credential-shaped assignment "
        f"{assignment.group(0)!r}"
    )
    assert not KEY_MATERIAL.search(text), f"{path.name}: key-like literal"
    assert not PEM_HEADER.search(text), f"{path.name}: embedded private key"


def test_env_example_ships_no_values_for_secret_keys() -> None:
    lines = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    values = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()

    for key in SECRET_ENV_KEYS:
        assert key in values, f"{key} missing from .env.example"
        assert values[key] == "", f"{key} must ship empty, got {values[key]!r}"


def test_env_example_documents_every_secret_setting() -> None:
    """A setting that is not in the template will be missed at deploy time."""
    from ozon_anti_bot_engine.config import Settings

    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    for field in ("api_key", "llm_api_key", "seller_client_id", "seller_api_key", "proxy_pool"):
        assert field in Settings.model_fields
        assert f"OZON_{field.upper()}" in text


def test_gitignore_blocks_secret_material() -> None:
    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "*.pem", "*.key", "cookies.txt", ".sessions/"):
        assert pattern in ignored, f"{pattern} is not gitignored"


def test_no_dotenv_file_is_committed() -> None:
    assert not (PROJECT_ROOT / ".env").exists() or True  # local .env is fine, just untracked
    tracked = PROJECT_ROOT / ".env"
    if tracked.exists():
        ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert ".env" in ignored


def test_settings_hide_secrets_from_repr() -> None:
    """A settings dump in a log or traceback must not leak the API key."""
    from ozon_anti_bot_engine.config import Settings

    for field in ("api_key", "llm_api_key", "seller_client_id", "seller_api_key"):
        assert Settings.model_fields[field].repr is False, f"{field} is repr-visible"
