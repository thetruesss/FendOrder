"""Proxy pool: parsing, credential redaction, rotation, bans and sticky sessions."""

from __future__ import annotations

import pytest

from ozon_anti_bot_engine.core.proxy_manager import ProxyEndpoint, ProxyPool

POOL = [
    "socks5://user:pass@10.0.0.1:1080",
    "socks5://user:pass@10.0.0.2:1080",
    "socks5://user:pass@10.0.0.3:1080",
]


# =============================================================================
#  Endpoint parsing
# =============================================================================
def test_parse_full_uri() -> None:
    endpoint = ProxyEndpoint.parse("socks5://alice:s3cret@1.2.3.4:1080")
    assert (endpoint.scheme, endpoint.host, endpoint.port) == ("socks5", "1.2.3.4", 1080)
    assert (endpoint.username, endpoint.password) == ("alice", "s3cret")
    assert endpoint.is_socks


def test_bare_host_port_defaults_to_socks5() -> None:
    assert ProxyEndpoint.parse("1.2.3.4:1080").scheme == "socks5"


def test_http_upstreams_are_supported() -> None:
    endpoint = ProxyEndpoint.parse("http://u:p@1.2.3.4:8080")
    assert not endpoint.is_socks
    assert endpoint.scheme == "http"


def test_percent_encoded_credentials_round_trip() -> None:
    endpoint = ProxyEndpoint.parse("socks5://user%40corp:pa%2Fss@1.2.3.4:1080")
    assert endpoint.username == "user@corp"
    assert endpoint.password == "pa/ss"
    # The rebuilt URI must re-escape, or curl will mis-split the authority.
    assert "user%40corp:pa%2Fss" in endpoint.uri


def test_credentials_never_appear_in_the_safe_uri() -> None:
    endpoint = ProxyEndpoint.parse("socks5://alice:supersecret@1.2.3.4:1080")
    assert "supersecret" not in endpoint.safe_uri
    assert "supersecret" not in str(endpoint)
    assert endpoint.safe_uri == "socks5://al***@1.2.3.4:1080"


@pytest.mark.parametrize(
    "bad", ["ftp://1.2.3.4:21", "socks5://1.2.3.4", "socks5://:1080", "not a proxy"]
)
def test_malformed_uris_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        ProxyEndpoint.parse(bad)


# =============================================================================
#  Pool behaviour
# =============================================================================
def test_empty_pool_degrades_to_direct_connection() -> None:
    pool = ProxyPool([])
    assert pool.is_empty
    assert pool.acquire() is None
    assert pool.acquire("session") is None
    pool.report_failure(None, 403)  # must not raise


def test_malformed_entries_are_skipped_not_fatal() -> None:
    pool = ProxyPool([POOL[0], "garbage", POOL[1]])
    assert len(pool) == 2


def test_acquire_is_round_robin() -> None:
    pool = ProxyPool(POOL)
    hosts = [pool.acquire().host for _ in range(6)]
    assert hosts == ["10.0.0.1", "10.0.0.2", "10.0.0.3"] * 2


def test_sticky_sessions_pin_one_exit_ip() -> None:
    pool = ProxyPool(POOL)
    first = pool.acquire("scrape-1")
    assert [pool.acquire("scrape-1") for _ in range(5)] == [first] * 5
    # A different chain gets a different exit.
    assert pool.acquire("scrape-2") != first


@pytest.mark.parametrize("status", [403, 429, 503])
def test_block_statuses_bench_the_proxy(status: int) -> None:
    pool = ProxyPool(POOL, cooldown_seconds=60)
    endpoint = pool.acquire()
    assert pool.report_failure(endpoint, status) is True
    assert pool.available_count == 2
    assert endpoint not in {pool.acquire() for _ in range(4)}


def test_transient_failures_only_bench_after_the_threshold() -> None:
    pool = ProxyPool(POOL, max_failures=3)
    endpoint = pool.acquire()
    assert pool.report_failure(endpoint, 500) is False
    assert pool.report_failure(endpoint, 500) is False
    assert pool.report_failure(endpoint, 500) is True
    assert pool.available_count == 2


def test_success_resets_the_failure_counter() -> None:
    pool = ProxyPool(POOL, max_failures=2)
    endpoint = pool.acquire()
    pool.report_failure(endpoint, 500)
    pool.report_success(endpoint)
    assert pool.report_failure(endpoint, 500) is False
    assert pool.available_count == 3


def test_benching_releases_the_sticky_pin() -> None:
    pool = ProxyPool(POOL, cooldown_seconds=60)
    pinned = pool.acquire("chain")
    pool.report_failure(pinned, 429)
    assert pool.acquire("chain") != pinned


def test_rotate_drops_the_pin_and_hands_out_the_next_exit() -> None:
    pool = ProxyPool(POOL)
    first = pool.acquire("chain")
    assert pool.rotate("chain") != first


def test_release_clears_the_sticky_lease() -> None:
    pool = ProxyPool(POOL)
    first = pool.acquire("chain")
    pool.release("chain")
    assert pool.acquire("chain") != first


def test_a_fully_benched_pool_force_releases_rather_than_failing_the_scrape() -> None:
    pool = ProxyPool(POOL, cooldown_seconds=3600)
    for _ in range(3):
        pool.report_failure(pool.acquire(), 403)
    assert pool.available_count == 0

    # A throttled proxy still beats no proxy at all.
    recovered = pool.acquire()
    assert recovered is not None


def test_stats_never_leak_credentials() -> None:
    pool = ProxyPool(["socks5://u:topsecret@10.0.0.9:1080"])
    stats = pool.stats()
    assert "topsecret" not in repr(stats)
    assert stats["total"] == 1
    assert stats["proxies"][0]["proxy"] == "socks5://u***@10.0.0.9:1080"
