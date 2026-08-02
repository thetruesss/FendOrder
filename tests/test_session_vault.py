"""Session vault: Netscape/JSON persistence and the 5-minute refresh margin."""

from __future__ import annotations

import time

import pytest

from ozon_anti_bot_engine.core.session_vault import (
    CookieRecord,
    SessionVault,
    dump_netscape,
    parse_netscape,
)

HOUR = 3600


def _cookie(name: str, value: str = "v", ttl: float | None = HOUR, **kwargs) -> CookieRecord:
    return CookieRecord(
        name=name,
        value=value,
        expires=None if ttl is None else time.time() + ttl,
        **kwargs,
    )


@pytest.fixture
def vault(tmp_path) -> SessionVault:
    return SessionVault(tmp_path, refresh_margin=300, watch_interval=1)


# =============================================================================
#  Cookie records
# =============================================================================
def test_expired_cookies_are_recognised() -> None:
    assert _cookie("a", ttl=-10).is_expired()
    assert not _cookie("a", ttl=10).is_expired()
    assert not _cookie("a", ttl=None).is_expired()  # session cookie never "expires"


def test_playwright_round_trip_preserves_flags() -> None:
    source = {
        "name": "__Secure-ETC",
        "value": "abc",
        "domain": ".ozon.ru",
        "path": "/",
        "expires": time.time() + HOUR,
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
    }
    record = CookieRecord.from_playwright(source)
    assert record.http_only and record.secure and record.same_site == "Lax"

    restored = record.to_playwright()
    assert restored["httpOnly"] is True
    assert restored["sameSite"] == "Lax"


def test_playwright_session_cookie_sentinel_is_decoded() -> None:
    record = CookieRecord.from_playwright({"name": "x", "value": "1", "expires": -1})
    assert record.is_session_cookie


# =============================================================================
#  Netscape format
# =============================================================================
def test_netscape_round_trip() -> None:
    cookies = [
        _cookie("__Secure-ETC", "etc-value", http_only=True),
        _cookie("xcid", "xcid-value"),
        _cookie("plain", "p", ttl=None),
    ]
    parsed = parse_netscape(dump_netscape(cookies))

    assert {c.name for c in parsed} == {"__Secure-ETC", "xcid", "plain"}
    by_name = {c.name: c for c in parsed}
    assert by_name["__Secure-ETC"].http_only  # the #HttpOnly_ prefix survives
    assert by_name["__Secure-ETC"].value == "etc-value"
    assert by_name["plain"].is_session_cookie


def test_netscape_header_and_comments_are_ignored() -> None:
    text = "# Netscape HTTP Cookie File\n# a comment\n\n.ozon.ru\tTRUE\t/\tTRUE\t0\tk\tv\n"
    parsed = parse_netscape(text)
    assert len(parsed) == 1
    assert (parsed[0].name, parsed[0].value) == ("k", "v")


def test_malformed_netscape_lines_are_skipped() -> None:
    assert parse_netscape("garbage\tnot\tenough\tfields\n") == []


# =============================================================================
#  Persistence
# =============================================================================
def test_save_and_load_round_trip(tmp_path) -> None:
    vault = SessionVault(tmp_path, cookie_format="both")
    vault.update(
        [_cookie("__Secure-ETC"), _cookie("xcid")],
        user_agent="UA/1.0",
        profile="chrome124",
        area_id=2,
    )

    assert (tmp_path / "default.session.json").is_file()
    assert (tmp_path / "default.cookies.txt").is_file()

    reloaded = SessionVault(tmp_path).load("default")
    assert reloaded is not None
    assert reloaded.user_agent == "UA/1.0"
    assert reloaded.area_id == 2
    assert {c.name for c in reloaded.cookies} == {"__Secure-ETC", "xcid"}


def test_expired_cookies_are_dropped_on_load(tmp_path) -> None:
    vault = SessionVault(tmp_path)
    vault.update([_cookie("fresh"), _cookie("stale", ttl=-HOUR)])
    assert {c.name for c in vault.cookies()} == {"fresh"}


def test_saved_session_file_is_not_world_readable(tmp_path) -> None:
    vault = SessionVault(tmp_path)
    vault.update([_cookie("__Secure-ETC")])
    mode = (tmp_path / "default.session.json").stat().st_mode & 0o777
    assert mode == 0o600  # cookies are credentials


def test_update_merges_rather_than_replaces(vault: SessionVault) -> None:
    vault.update([_cookie("a", "1")])
    vault.update([_cookie("b", "2")])
    assert vault.cookie_dict() == {"a": "1", "b": "2"}

    vault.update([_cookie("a", "updated")])
    assert vault.cookie_dict()["a"] == "updated"


def test_cookie_header_rendering(vault: SessionVault) -> None:
    vault.update([_cookie("a", "1"), _cookie("b", "2")])
    assert vault.cookie_header() == "a=1; b=2"


def test_import_and_export_netscape(tmp_path, vault: SessionVault) -> None:
    source = tmp_path / "in.txt"
    source.write_text(dump_netscape([_cookie("xcid", "from-file")]), encoding="utf-8")

    vault.import_netscape(source)
    assert vault.cookie_dict()["xcid"] == "from-file"

    exported = vault.export_netscape(tmp_path / "out.txt")
    assert "from-file" in exported.read_text(encoding="utf-8")


def test_corrupt_session_file_does_not_crash_the_vault(tmp_path) -> None:
    (tmp_path / "default.session.json").write_text("{not json", encoding="utf-8")
    assert SessionVault(tmp_path).load("default") is None


# =============================================================================
#  Freshness (spec §3.3)
# =============================================================================
def test_missing_critical_cookies_trigger_a_refresh(vault: SessionVault) -> None:
    assert vault.needs_refresh()
    assert set(vault.missing_critical()) == {"__Secure-ETC", "__Secure-ab-group", "xcid"}

    vault.update([_cookie("__Secure-ETC"), _cookie("xcid")])
    assert not vault.needs_refresh()
    # ab-group is optional: it is an A/B bucket and is not always issued.
    assert vault.missing_critical() == ["__Secure-ab-group"]


def test_refresh_fires_inside_the_five_minute_margin(vault: SessionVault) -> None:
    vault.update([_cookie("__Secure-ETC", ttl=299), _cookie("xcid", ttl=HOUR)])
    assert vault.needs_refresh()

    vault.clear()
    vault.update([_cookie("__Secure-ETC", ttl=301), _cookie("xcid", ttl=HOUR)])
    assert not vault.needs_refresh()


def test_seconds_to_expiry_reports_the_earliest_critical_cookie(vault: SessionVault) -> None:
    vault.update([_cookie("__Secure-ETC", ttl=2 * HOUR), _cookie("xcid", ttl=600)])
    assert vault.seconds_to_expiry() == pytest.approx(600, abs=5)


async def test_refresh_now_invokes_the_registered_refresher(vault: SessionVault) -> None:
    calls: list[int] = []

    async def refresher() -> None:
        calls.append(1)
        vault.update([_cookie("__Secure-ETC"), _cookie("xcid")])

    vault.register_refresher(refresher)
    assert await vault.refresh_now()
    assert calls == [1]
    assert vault.refresh_count == 1
    assert not vault.needs_refresh()

    # Already fresh — the guarded path must not launch a second browser.
    assert await vault.refresh_now()
    assert calls == [1]


async def test_refresh_failure_is_reported_not_raised(vault: SessionVault) -> None:
    async def broken() -> None:
        raise RuntimeError("browser died")

    vault.register_refresher(broken)
    assert await vault.refresh_now() is False
    assert vault.refresh_count == 0


async def test_watcher_starts_and_stops_cleanly(vault: SessionVault) -> None:
    vault.start_watch()
    assert vault.stats()["watcher_running"]
    await vault.stop_watch()
    assert not vault.stats()["watcher_running"]


def test_stats_snapshot_shape(vault: SessionVault) -> None:
    vault.update([_cookie("__Secure-ETC"), _cookie("xcid")])
    stats = vault.stats()
    assert stats["cookies"] == 2
    assert stats["needs_refresh"] is False
    assert "seconds_to_expiry" in stats
