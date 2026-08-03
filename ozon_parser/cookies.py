"""Load an authorised Ozon session from ``cookies.txt``.

Three on-disk formats are accepted so the file can come from a browser export,
a curl jar or a hand-written snippet:

* Netscape / Mozilla ``cookies.txt`` (``domain\\tflag\\tpath\\tsecure\\texpiry\\tname\\tvalue``)
* a JSON array of cookie objects (e.g. an ``EditThisCookie`` export)
* a flat ``name=value; name2=value2`` header string

The result is a list of Playwright ``add_cookies`` dictionaries scoped to
``.ozon.ru``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_DEFAULT_DOMAIN = ".ozon.ru"


def load_cookies(
    path: str | Path,
    *,
    default_domain: str = _DEFAULT_DOMAIN,
    required: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Parse *path* and return Playwright-ready cookie dicts.

    Raises :class:`FileNotFoundError` if the file is missing and
    :class:`ValueError` if it is empty, unparseable, or missing a required
    cookie name.
    """

    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Cookie file not found: {file_path}")

    raw = file_path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        raise ValueError(f"Cookie file is empty: {file_path}")

    cookies = _parse_json(raw, default_domain)
    if cookies is None:
        cookies = _parse_netscape(raw, default_domain)
    if not cookies:
        cookies = _parse_header_string(raw, default_domain)

    if not cookies:
        raise ValueError(f"No cookies could be parsed from {file_path}")

    _check_required(cookies, required, file_path)
    logger.info("Loaded %d cookies from %s", len(cookies), file_path)
    return cookies


# -- format parsers --------------------------------------------------------


def _parse_json(raw: str, default_domain: str) -> list[dict[str, Any]] | None:
    if not raw.lstrip().startswith(("[", "{")):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if isinstance(data, dict):
        data = data.get("cookies", [])
    if not isinstance(data, list):
        return None

    cookies: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if not name or value is None:
            continue
        cookies.append(
            _build_cookie(
                str(name),
                str(value),
                domain=str(item.get("domain") or default_domain),
                path=str(item.get("path") or "/"),
                secure=item.get("secure"),
                http_only=item.get("httpOnly"),
            )
        )
    return cookies


def _parse_netscape(raw: str, default_domain: str) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        # ``#HttpOnly_`` is a real prefix in curl jars; keep those lines.
        if not stripped or (stripped.startswith("#") and not stripped.startswith("#HttpOnly_")):
            continue

        line = line.replace("#HttpOnly_", "", 1)
        fields = line.split("\t")
        if len(fields) < 7:
            continue

        domain, _flag, cookie_path, secure, _expiry, name, value = fields[:7]
        if not name:
            continue
        cookies.append(
            _build_cookie(
                name.strip(),
                value.strip(),
                domain=domain.strip() or default_domain,
                path=cookie_path.strip() or "/",
                secure=secure.strip().upper() == "TRUE",
            )
        )
    return cookies


def _parse_header_string(raw: str, default_domain: str) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    # Support both "a=1; b=2" on one line and one pair per line.
    parts = [p for chunk in raw.splitlines() for p in chunk.split(";")]
    for part in parts:
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if not name:
            continue
        cookies.append(_build_cookie(name, value.strip(), domain=default_domain, path="/"))
    return cookies


# -- helpers ---------------------------------------------------------------


def _build_cookie(
    name: str,
    value: str,
    *,
    domain: str,
    path: str,
    secure: bool | None = None,
    http_only: bool | None = None,
) -> dict[str, Any]:
    # ``__Secure-``/``__Host-`` prefixed cookies are only valid when secure.
    if secure is None:
        secure = name.startswith(("__Secure-", "__Host-"))

    cookie: dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path or "/",
        "secure": bool(secure),
        "sameSite": "Lax",
    }
    if http_only is not None:
        cookie["httpOnly"] = bool(http_only)
    return cookie


def _check_required(
    cookies: list[dict[str, Any]], required: Iterable[str], file_path: Path
) -> None:
    present = {c["name"] for c in cookies}
    missing = [name for name in required if name not in present]
    if missing:
        raise ValueError(
            f"{file_path} is missing required cookie(s): {', '.join(missing)}. "
            "An authorised session needs all of them."
        )
