"""Anti-bot primitives: TLS profiles, header synthesis, WAF challenge handling.

Three responsibilities live here:

1. **TLS / browser profiles** — the ClientHello identity handed to ``curl_cffi``
   plus the HTTP headers that must agree with it. A Chrome-124 JA3 paired with a
   Firefox ``User-Agent`` is a louder signal than no spoofing at all, so the
   profile owns both halves and they are never mixed.
2. **Challenge detection** — recognising a Fabrique/WAF interstitial in an HTTP
   response *before* the parser trips over HTML it expected to be JSON.
3. **Proof-of-Work solving** — a native solver for the hashcash-style challenge,
   with an explicit escalation path to the real browser when the challenge is
   not one we can evaluate offline.

Design note on "100% bypass": no library can guarantee that against a WAF that
is updated adversarially. What this module guarantees is *detection* — every
challenge is surfaced as a typed verdict, and the requester escalates to a real
Gecko engine rather than silently returning garbage.
"""

from __future__ import annotations

import hashlib
import random
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

# Cookies that must be present and fresh for the fast HTTP path to work.
CRITICAL_COOKIES: tuple[str, ...] = ("__Secure-ETC", "__Secure-ab-group", "xcid")

# Ozon's Composer API is versioned through this header; it is checked server-side.
OZON_APP_NAME = "dweb_client"
OZON_APP_VERSION = "1.0.0"


# =============================================================================
#  TLS / browser identity
# =============================================================================
@dataclass(frozen=True, slots=True)
class TLSProfile:
    """A coherent (TLS fingerprint, User-Agent, client-hints) triple."""

    name: str
    impersonate: str  # curl_cffi target
    user_agent: str
    sec_ch_ua: str
    sec_ch_ua_platform: str
    sec_ch_ua_mobile: str = "?0"
    accept_language: str = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"


PROFILES: dict[str, TLSProfile] = {
    "chrome124": TLSProfile(
        name="chrome124",
        impersonate="chrome124",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        sec_ch_ua='"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        sec_ch_ua_platform='"Windows"',
    ),
    "chrome120": TLSProfile(
        name="chrome120",
        impersonate="chrome120",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        sec_ch_ua='"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        sec_ch_ua_platform='"Windows"',
    ),
    "chrome116": TLSProfile(
        name="chrome116",
        impersonate="chrome116",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
        ),
        sec_ch_ua='"Not)A;Brand";v="24", "Chromium";v="116", "Google Chrome";v="116"',
        sec_ch_ua_platform='"macOS"',
    ),
}

DEFAULT_PROFILE = "chrome124"


def get_profile(name: str | None = None) -> TLSProfile:
    """Resolve a profile by name, falling back to the default rather than raising."""
    if not name:
        return PROFILES[DEFAULT_PROFILE]
    return PROFILES.get(name.lower(), PROFILES[DEFAULT_PROFILE])


# =============================================================================
#  Header synthesis
# =============================================================================
class HeaderFactory:
    """Builds header sets whose *order* and content match the active TLS profile.

    Header ordering is part of the fingerprint. Python dicts preserve insertion
    order and ``curl_cffi`` transmits them in that order, so the literal layout
    of these dicts is load-bearing — do not alphabetise them.
    """

    def __init__(self, profile: TLSProfile | None = None) -> None:
        self.profile = profile or get_profile()

    # --- Top-level HTML navigation ------------------------------------------
    def document(self, referer: str | None = None) -> dict[str, str]:
        headers = {
            "sec-ch-ua": self.profile.sec_ch_ua,
            "sec-ch-ua-mobile": self.profile.sec_ch_ua_mobile,
            "sec-ch-ua-platform": self.profile.sec_ch_ua_platform,
            "upgrade-insecure-requests": "1",
            "user-agent": self.profile.user_agent,
            "accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
            ),
            "sec-fetch-site": "same-origin" if referer else "none",
            "sec-fetch-mode": "navigate",
            "sec-fetch-user": "?1",
            "sec-fetch-dest": "document",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": self.profile.accept_language,
        }
        if referer:
            headers["referer"] = referer
        return headers

    # --- Composer API (XHR) --------------------------------------------------
    def composer(
        self,
        referer: str,
        *,
        page_type: str = "pdp",
        extra: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        headers = {
            "accept": "application/json",
            "accept-language": self.profile.accept_language,
            "sec-ch-ua": self.profile.sec_ch_ua,
            "sec-ch-ua-mobile": self.profile.sec_ch_ua_mobile,
            "sec-ch-ua-platform": self.profile.sec_ch_ua_platform,
            "user-agent": self.profile.user_agent,
            "x-o3-app-name": OZON_APP_NAME,
            "x-o3-app-version": OZON_APP_VERSION,
            "x-o3-page-type": page_type,
            "x-requested-with": "XMLHttpRequest",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": referer,
            "accept-encoding": "gzip, deflate, br, zstd",
        }
        if extra:
            headers.update(extra)
        return headers


# =============================================================================
#  Challenge detection
# =============================================================================
class ChallengeKind(StrEnum):
    NONE = "none"
    JS_POW = "js_pow"           # solvable JavaScript proof-of-work
    INTERSTITIAL = "interstitial"  # "checking your browser" page, browser required
    CAPTCHA = "captcha"         # human-interaction challenge
    RATE_LIMIT = "rate_limit"   # 429 / 503 — rotate, do not re-solve
    FORBIDDEN = "forbidden"     # 403 — IP or fingerprint burnt


# Markers observed on Ozon/Fabrique interstitials. Kept deliberately broad: a
# false positive costs one browser round-trip, a false negative costs a parse
# error and a silently empty result.
_CHALLENGE_MARKERS: tuple[str, ...] = (
    "fabrique",
    "challenge.js",
    "__wafchlg",
    "window.__challenge",
    'id="challenge',
    "checking your browser",
    "проверяем ваш браузер",
    "доступ ограничен",
    "антибот",
    "/api/challenge",
    "please enable javascript and cookies",
)

_CAPTCHA_MARKERS: tuple[str, ...] = (
    "smartcaptcha",
    "yandex.ru/captcha",
    "g-recaptcha",
    "hcaptcha",
    "captcha-widget",
)

# Rate-limit / block statuses that must trigger proxy rotation (spec §3.2).
ROTATE_ON_STATUS: frozenset[int] = frozenset({403, 429, 503})


@dataclass(slots=True)
class ChallengeVerdict:
    kind: ChallengeKind = ChallengeKind.NONE
    status: int = 200
    detail: str = ""
    pow_challenge: PowChallenge | None = None

    @property
    def blocked(self) -> bool:
        return self.kind is not ChallengeKind.NONE

    @property
    def needs_browser(self) -> bool:
        """True when only a real JS engine can clear this."""
        return self.kind in (ChallengeKind.INTERSTITIAL, ChallengeKind.CAPTCHA)

    @property
    def needs_rotation(self) -> bool:
        """True when the exit IP / fingerprint should be swapped."""
        return self.kind in (ChallengeKind.RATE_LIMIT, ChallengeKind.FORBIDDEN)


class ChallengeDetector:
    """Classifies an HTTP response as clean, challenged, throttled or blocked."""

    def inspect(
        self,
        status: int,
        body: str | bytes,
        headers: Mapping[str, str] | None = None,
    ) -> ChallengeVerdict:
        headers = {k.lower(): v for k, v in (headers or {}).items()}

        if status in (429, 503):
            return ChallengeVerdict(ChallengeKind.RATE_LIMIT, status, f"HTTP {status}")

        text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else body
        lowered = text[:20_000].lower()

        if any(marker in lowered for marker in _CAPTCHA_MARKERS):
            return ChallengeVerdict(ChallengeKind.CAPTCHA, status, "captcha widget present")

        if any(marker in lowered for marker in _CHALLENGE_MARKERS):
            pow_challenge = PowChallenge.from_html(text)
            if pow_challenge is not None:
                return ChallengeVerdict(
                    ChallengeKind.JS_POW, status, "solvable PoW", pow_challenge
                )
            return ChallengeVerdict(
                ChallengeKind.INTERSTITIAL, status, "WAF interstitial, browser required"
            )

        if status == 403:
            return ChallengeVerdict(ChallengeKind.FORBIDDEN, status, "HTTP 403")

        # A JSON endpoint that answers with HTML is a challenge we failed to
        # fingerprint by marker — treat it as an interstitial rather than
        # letting the JSON parser raise.
        content_type = headers.get("content-type", "")
        if "json" in content_type and lowered.lstrip().startswith("<!doctype html"):
            return ChallengeVerdict(
                ChallengeKind.INTERSTITIAL, status, "HTML served for a JSON endpoint"
            )

        return ChallengeVerdict(ChallengeKind.NONE, status)


# =============================================================================
#  Proof-of-Work
# =============================================================================
_POW_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "salt",
        re.compile(
            r"""["']?(?:salt|challenge|prefix|seed)["']?\s*[:=]\s*"""
            r"""["']([A-Za-z0-9+/=_\-]{4,128})["']""",
            re.I,
        ),
    ),
    (
        "difficulty",
        re.compile(
            r"""["']?(?:difficulty|complexity|zeros|leadingZeros)["']?\s*[:=]\s*"""
            r"""["']?(\d{1,3})["']?""",
            re.I,
        ),
    ),
    (
        "algorithm",
        re.compile(
            r"""["']?(?:algorithm|algo|hash)["']?\s*[:=]\s*"""
            r"""["'](sha-?256|sha-?1|md5)["']""",
            re.I,
        ),
    ),
)

_SUBMIT_URL_PATTERN = re.compile(
    r"""["']?(?:submitUrl|action|verifyUrl|endpoint)["']?\s*[:=]\s*["'](/[^"']{2,200})["']""",
    re.I,
)

_HASHERS = {
    "sha256": hashlib.sha256,
    "sha1": hashlib.sha1,
    "md5": hashlib.md5,
}


@dataclass(slots=True)
class PowChallenge:
    """A hashcash-style challenge: find ``nonce`` s.t. ``H(salt+nonce)`` is small."""

    salt: str
    difficulty: int = 4
    algorithm: str = "sha256"
    submit_url: str | None = None
    raw: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_html(cls, html: str) -> PowChallenge | None:
        """Best-effort extraction of PoW parameters from an interstitial page.

        Returns ``None`` when the page does not expose parameters we can
        evaluate offline — the caller then escalates to the browser, which runs
        the real script instead of us re-implementing it.
        """
        window = html[:60_000]
        found: dict[str, str] = {}
        for key, pattern in _POW_PATTERNS:
            match = pattern.search(window)
            if match:
                found[key] = match.group(1)

        if "salt" not in found:
            return None

        algorithm = found.get("algorithm", "sha256").replace("-", "").lower()
        if algorithm not in _HASHERS:
            return None

        submit = _SUBMIT_URL_PATTERN.search(window)
        return cls(
            salt=found["salt"],
            difficulty=int(found.get("difficulty", 4)),
            algorithm=algorithm,
            submit_url=submit.group(1) if submit else None,
            raw=found,
        )


@dataclass(slots=True)
class PowSolution:
    nonce: int
    digest: str
    attempts: int
    elapsed: float


class ProofOfWorkSolver:
    """Native solver for leading-zero hashcash challenges.

    Ozon's Fabrique screens have historically used a *leading zero nibbles*
    variant: ``H(salt + nonce)`` must begin with ``difficulty`` hex zeros. Both
    the nibble and the bit interpretation are supported because the two are
    trivially confusable and getting it wrong just wastes CPU.
    """

    def __init__(self, max_attempts: int = 20_000_000, time_budget: float = 30.0) -> None:
        self.max_attempts = max_attempts
        self.time_budget = time_budget

    def solve(self, challenge: PowChallenge, *, bitwise: bool = False) -> PowSolution | None:
        hasher = _HASHERS[challenge.algorithm]
        salt = challenge.salt.encode()
        difficulty = max(0, challenge.difficulty)
        started = time.monotonic()

        if bitwise:
            target_bytes = difficulty // 8
            remainder_bits = difficulty % 8
            for nonce in range(self.max_attempts):
                if nonce % 8192 == 0 and time.monotonic() - started > self.time_budget:
                    return None
                digest = hasher(salt + str(nonce).encode()).digest()
                if digest[:target_bytes] != b"\x00" * target_bytes:
                    continue
                if remainder_bits and digest[target_bytes] >> (8 - remainder_bits):
                    continue
                return PowSolution(
                    nonce, digest.hex(), nonce + 1, time.monotonic() - started
                )
            return None

        prefix = "0" * difficulty
        for nonce in range(self.max_attempts):
            if nonce % 8192 == 0 and time.monotonic() - started > self.time_budget:
                return None
            digest = hasher(salt + str(nonce).encode()).hexdigest()
            if digest.startswith(prefix):
                return PowSolution(nonce, digest, nonce + 1, time.monotonic() - started)
        return None


# =============================================================================
#  Politeness
# =============================================================================
def jitter(low: float, high: float) -> float:
    """Randomised inter-request delay.

    Constant-interval traffic is one of the cheapest bot signals to mine, so the
    scrapers always sleep a jittered amount rather than a fixed one. It is also
    simply the polite way to consume someone else's origin.
    """
    if high <= low:
        return max(0.0, low)
    return random.uniform(low, high)


def cookie_names(cookies: Iterable[Mapping[str, str]]) -> set[str]:
    return {c.get("name", "") for c in cookies}


def has_critical_cookies(cookies: Iterable[Mapping[str, str]]) -> bool:
    """True when the session carries the cookies the fast HTTP path depends on.

    ``__Secure-ab-group`` is treated as optional: it is an A/B bucket that is not
    always issued, and demanding it would cause needless browser round-trips.
    """
    names = cookie_names(cookies)
    return "__Secure-ETC" in names and "xcid" in names
