"""Anti-bot core: challenge classification, PoW solving, fingerprint coherence."""

from __future__ import annotations

import hashlib

import pytest

from ozon_anti_bot_engine.core.anti_bot import (
    CRITICAL_COOKIES,
    ChallengeDetector,
    ChallengeKind,
    HeaderFactory,
    PowChallenge,
    ProofOfWorkSolver,
    get_profile,
    has_critical_cookies,
    jitter,
)

detector = ChallengeDetector()

POW_HTML = """
<html><head><script src="/fabrique/challenge.js"></script></head>
<body><script>
  var config = {"salt":"a1b2c3d4","difficulty":"3","algorithm":"sha256"};
  var submitUrl = "/api/challenge/verify";
</script></body></html>
"""


# =============================================================================
#  Challenge classification
# =============================================================================
@pytest.mark.parametrize("status", [429, 503])
def test_throttling_is_classified_as_rate_limit(status: int) -> None:
    verdict = detector.inspect(status, "")
    assert verdict.kind is ChallengeKind.RATE_LIMIT
    assert verdict.needs_rotation
    assert not verdict.needs_browser


def test_plain_403_burns_the_exit_ip() -> None:
    verdict = detector.inspect(403, "Access denied")
    assert verdict.kind is ChallengeKind.FORBIDDEN
    assert verdict.needs_rotation


def test_waf_interstitial_requires_a_real_browser() -> None:
    verdict = detector.inspect(200, "<html><body>Проверяем ваш браузер…</body></html>")
    assert verdict.kind is ChallengeKind.INTERSTITIAL
    assert verdict.needs_browser
    assert not verdict.needs_rotation


def test_solvable_pow_is_detected_with_its_parameters() -> None:
    verdict = detector.inspect(200, POW_HTML)
    assert verdict.kind is ChallengeKind.JS_POW
    assert verdict.pow_challenge is not None
    assert verdict.pow_challenge.salt == "a1b2c3d4"
    assert verdict.pow_challenge.difficulty == 3


def test_captcha_is_distinguished_from_a_pow_challenge() -> None:
    verdict = detector.inspect(200, '<div class="g-recaptcha"></div>')
    assert verdict.kind is ChallengeKind.CAPTCHA
    assert verdict.needs_browser


def test_clean_json_passes_through() -> None:
    verdict = detector.inspect(200, '{"widgetStates":{"webPrice-1":"{}"}}')
    assert verdict.kind is ChallengeKind.NONE
    assert not verdict.blocked


def test_html_served_for_a_json_endpoint_is_treated_as_a_challenge() -> None:
    """The failure mode that otherwise surfaces as a confusing JSON decode error."""
    verdict = detector.inspect(
        200,
        "<!DOCTYPE html><html><body>anything</body></html>",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert verdict.kind is ChallengeKind.INTERSTITIAL


def test_bytes_bodies_are_accepted() -> None:
    verdict = detector.inspect(200, "Проверяем ваш браузер".encode())
    assert verdict.kind is ChallengeKind.INTERSTITIAL


# =============================================================================
#  Proof of work
# =============================================================================
def test_pow_parameters_are_parsed_from_the_interstitial() -> None:
    challenge = PowChallenge.from_html(POW_HTML)
    assert challenge is not None
    assert challenge.algorithm == "sha256"
    assert challenge.submit_url == "/api/challenge/verify"


def test_pages_without_parseable_parameters_return_none_so_we_escalate() -> None:
    assert PowChallenge.from_html("<html>checking your browser</html>") is None


def test_solver_finds_a_valid_nonce() -> None:
    challenge = PowChallenge(salt="a1b2c3d4", difficulty=3)
    solution = ProofOfWorkSolver().solve(challenge)

    assert solution is not None
    digest = hashlib.sha256(b"a1b2c3d4" + str(solution.nonce).encode()).hexdigest()
    assert digest.startswith("000")
    assert digest == solution.digest


def test_solver_supports_the_leading_bits_interpretation() -> None:
    challenge = PowChallenge(salt="seed", difficulty=8)
    solution = ProofOfWorkSolver().solve(challenge, bitwise=True)

    assert solution is not None
    digest = hashlib.sha256(b"seed" + str(solution.nonce).encode()).digest()
    assert digest[0] == 0


def test_solver_gives_up_within_its_budget_instead_of_hanging() -> None:
    # 16 leading hex zeros is computationally out of reach; the caller must get
    # None back promptly so it can escalate to the browser.
    challenge = PowChallenge(salt="x", difficulty=16)
    assert ProofOfWorkSolver(time_budget=1.0).solve(challenge) is None


# =============================================================================
#  Fingerprint coherence
# =============================================================================
def test_profile_ua_and_client_hints_agree() -> None:
    profile = get_profile("chrome124")
    assert "Chrome/124" in profile.user_agent
    assert 'v="124"' in profile.sec_ch_ua
    assert profile.impersonate == "chrome124"


def test_unknown_profile_falls_back_rather_than_raising() -> None:
    assert get_profile("netscape-navigator").name == "chrome124"
    assert get_profile(None).name == "chrome124"


def test_document_headers_look_like_a_navigation() -> None:
    headers = HeaderFactory().document()
    assert headers["sec-fetch-dest"] == "document"
    assert headers["sec-fetch-site"] == "none"
    assert "text/html" in headers["accept"]
    assert "referer" not in headers


def test_referer_flips_sec_fetch_site_to_same_origin() -> None:
    headers = HeaderFactory().document(referer="https://www.ozon.ru/")
    assert headers["sec-fetch-site"] == "same-origin"
    assert headers["referer"] == "https://www.ozon.ru/"


def test_composer_headers_carry_the_ozon_client_markers() -> None:
    headers = HeaderFactory().composer("https://www.ozon.ru/product/1", page_type="reviews")
    assert headers["x-o3-app-name"] == "dweb_client"
    assert headers["x-o3-page-type"] == "reviews"
    assert headers["x-requested-with"] == "XMLHttpRequest"
    assert headers["sec-fetch-dest"] == "empty"
    assert headers["accept"] == "application/json"


def test_headers_never_contradict_the_tls_profile() -> None:
    """A Chrome JA3 with a Firefox UA is a louder signal than no spoofing at all."""
    profile = get_profile("chrome120")
    factory = HeaderFactory(profile)
    for headers in (factory.document(), factory.composer("https://www.ozon.ru/")):
        assert headers["user-agent"] == profile.user_agent
        assert headers["sec-ch-ua"] == profile.sec_ch_ua


def test_header_order_is_preserved_because_it_is_part_of_the_fingerprint() -> None:
    keys = list(HeaderFactory().document())
    assert keys[0] == "sec-ch-ua"
    assert keys.index("user-agent") < keys.index("accept")


# =============================================================================
#  Cookies & pacing
# =============================================================================
def test_critical_cookie_check_ignores_the_optional_ab_group() -> None:
    assert has_critical_cookies([{"name": "__Secure-ETC"}, {"name": "xcid"}])
    assert not has_critical_cookies([{"name": "__Secure-ETC"}])
    assert not has_critical_cookies([])
    assert "__Secure-ab-group" in CRITICAL_COOKIES


def test_jitter_stays_within_bounds() -> None:
    assert all(0.2 <= jitter(0.2, 0.9) <= 0.9 for _ in range(200))
    assert jitter(0.5, 0.5) == 0.5
    assert jitter(1.0, 0.1) == 1.0  # inverted bounds must not produce a negative sleep
