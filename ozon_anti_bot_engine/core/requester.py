"""Unified hybrid requester: Camoufox stealth browser + curl_cffi async HTTP.

Two transports, one session (spec §3.1):

* **Stealth browser (Camoufox / Gecko).** Used for the first touch on a card and
  whenever the WAF interposes. Camoufox patches fingerprints in C++ inside the
  Gecko engine rather than injecting JavaScript, so the usual
  ``navigator.webdriver``/property-descriptor probes find nothing to flag. It
  runs the real Fabrique proof-of-work and yields hot cookies.
* **Ultra-fast async HTTP (curl_cffi).** Used for bulk Composer API pagination on
  those cookies, impersonating a real Chrome ClientHello. Roughly 4× cheaper in
  proxy traffic than driving a browser for every page, which is the whole point
  of the hybrid design.

Escalation is automatic: the HTTP path detects a challenge, hands control back
to the browser, and resumes once cookies are hot again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from ..config import Settings, get_settings
from .anti_bot import (
    ChallengeDetector,
    ChallengeKind,
    HeaderFactory,
    ProofOfWorkSolver,
    get_profile,
    jitter,
)
from .proxy_manager import HttpToSocks5Bridge, ProxyEndpoint, ProxyPool
from .session_vault import SessionVault

logger = logging.getLogger(__name__)

OZON_ORIGIN = "https://www.ozon.ru"
COMPOSER_V2 = f"{OZON_ORIGIN}/api/composer-api.bx/page/json/v2"


# =============================================================================
#  URL construction
# =============================================================================
def normalise_path(path: str) -> str:
    """Strip the trailing slash that sits directly before a query string.

    This is the single most expensive footgun in the whole Ozon API (spec §4.1):

        /product/<SKU>/reviews/?sort=score_asc&page=2   -> 307 redirect to page 1
        /product/<SKU>/reviews?sort=score_asc&page=2    -> correct page 2

    The redirect is silent and lands on page 1, so a scraper that gets this
    wrong collects page 1 five hundred times and reports a healthy-looking run.
    Every URL built in this package goes through here.
    """
    if "?" not in path:
        return path
    head, _, query = path.partition("?")
    return f"{head.rstrip('/')}?{query}" if head != "/" else path


def has_slash_before_query(path: str) -> bool:
    """Test helper: ``True`` when ``path`` carries the 307-triggering slash."""
    return "/?" in path


class OzonUrls:
    """Builders for every Ozon path the engine touches."""

    @staticmethod
    def product(sku: str | int, area_id: int | None = None) -> str:
        path = f"/product/{sku}"
        if area_id is not None:
            path = f"{path}?areaId={area_id}"
        return normalise_path(path)

    @staticmethod
    def product_url(sku: str | int, area_id: int | None = None) -> str:
        """Absolute PDP URL — used for the physical region navigation (spec §4.2)."""
        return f"{OZON_ORIGIN}{OzonUrls.product(sku, area_id)}"

    @staticmethod
    def reviews(
        sku: str | int,
        *,
        sort: str = "score_desc",
        page: int = 1,
        area_id: int | None = None,
    ) -> str:
        params: dict[str, str | int] = {"sort": sort}
        if page > 1:
            params["page"] = page
        if area_id is not None:
            params["areaId"] = area_id
        # No slash before '?' — see normalise_path().
        return normalise_path(f"/product/{sku}/reviews?{urlencode(params)}")

    @staticmethod
    def search(query: str, *, page: int = 1, area_id: int | None = None) -> str:
        params: dict[str, str | int] = {"text": query, "from_global": "true"}
        if page > 1:
            params["page"] = page
        if area_id is not None:
            params["areaId"] = area_id
        return normalise_path(f"/search?{urlencode(params)}")

    @staticmethod
    def composer(site_path: str) -> str:
        """Wrap a site path into a Composer API v2 call."""
        return f"{COMPOSER_V2}?url={quote(normalise_path(site_path), safe='')}"


# =============================================================================
#  Composer response helpers
# =============================================================================
def parse_widget_states(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Decode ``widgetStates``: Ozon nests JSON *as strings* inside the response."""
    import json

    states: dict[str, Any] = {}
    raw = payload.get("widgetStates") or {}
    if not isinstance(raw, Mapping):
        return states
    for key, value in raw.items():
        if isinstance(value, str):
            try:
                states[key] = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                states[key] = value
        else:
            states[key] = value
    return states


def iter_widgets(
    payload: Mapping[str, Any], *prefixes: str
) -> Iterator[tuple[str, Any]]:
    """Yield ``(key, decoded_state)`` for widgets whose key starts with a prefix.

    Widget keys carry an instance suffix (``webListReviews-3311601-default-1``),
    so matching is by prefix and callers must tolerate several instances.
    """
    states = parse_widget_states(payload)
    for key, value in states.items():
        name = key.split("-", 1)[0]
        if not prefixes or name in prefixes or any(key.startswith(p) for p in prefixes):
            yield key, value


def first_widget(payload: Mapping[str, Any], *prefixes: str) -> Any | None:
    for _key, value in iter_widgets(payload, *prefixes):
        return value
    return None


def deep_get(obj: Any, *keys: str, default: Any = None) -> Any:
    """Tolerant nested lookup: ``deep_get(d, 'price', 'value')``."""
    cursor = obj
    for key in keys:
        if isinstance(cursor, Mapping) and key in cursor:
            cursor = cursor[key]
        else:
            return default
    return cursor


def find_key(obj: Any, *names: str, max_depth: int = 8) -> Any | None:
    """Breadth-first search for the first occurrence of any of ``names``.

    Ozon reshuffles widget internals often enough that pinning exact paths is a
    losing game; searching by key name survives most layout churn.
    """
    wanted = set(names)
    queue: list[tuple[Any, int]] = [(obj, 0)]
    while queue:
        node, depth = queue.pop(0)
        if depth > max_depth:
            continue
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key in wanted:
                    return value
            queue.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, (list, tuple)):
            queue.extend((v, depth + 1) for v in node)
    return None


# =============================================================================
#  Results
# =============================================================================
@dataclass(slots=True)
class BrowserCapture:
    """What one physical browser navigation produced."""

    url: str
    final_url: str
    html: str
    captured: list[dict[str, Any]] = field(default_factory=list)
    cookies: list[dict[str, Any]] = field(default_factory=list)
    challenge_cleared: bool = False
    duration: float = 0.0


class RequesterError(RuntimeError):
    """Raised when a request could not be completed after every escalation."""


# =============================================================================
#  Hybrid requester
# =============================================================================
class HybridRequester:
    """Owns the proxy bridge, the proxy pool, the cookie vault and both clients."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        vault: SessionVault | None = None,
        pool: ProxyPool | None = None,
        bridge: HttpToSocks5Bridge | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.profile = get_profile(self.settings.impersonate_profile)
        self.headers = HeaderFactory(self.profile)
        self.detector = ChallengeDetector()
        self.pow_solver = ProofOfWorkSolver()

        self.vault = vault or SessionVault(
            self.settings.session_dir,
            refresh_margin=self.settings.cookie_refresh_margin,
            watch_interval=self.settings.cookie_watch_interval,
            cookie_format=self.settings.cookie_format,
        )
        self.pool = pool or ProxyPool(
            self.settings.proxy_uris,
            cooldown_seconds=self.settings.proxy_cooldown_seconds,
            sticky_ttl_seconds=self.settings.proxy_sticky_ttl_seconds,
        )
        self.bridge = bridge or HttpToSocks5Bridge(
            host=self.settings.proxy_bridge_host,
            port=self.settings.proxy_bridge_port,
            max_threads=self.settings.proxy_bridge_max_threads,
        )

        self._http: Any | None = None
        self._semaphore = asyncio.Semaphore(self.settings.http_concurrency)
        self._last_request_at = 0.0
        self._started = False

        # Telemetry
        self.stats_http_requests = 0
        self.stats_browser_launches = 0
        self.stats_challenges = 0
        self.stats_rotations = 0
        self.stats_pow_solved = 0

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._started:
            return
        if not self.pool.is_empty:
            self.bridge.set_upstream(self.pool.acquire("bridge"))
            self.bridge.start()
        else:
            logger.warning(
                "no proxies configured — running direct. Ozon will rate-limit a "
                "datacenter IP quickly; set OZON_PROXY_POOL for real workloads."
            )

        from curl_cffi import requests as cffi_requests

        self._http = cffi_requests.AsyncSession(
            impersonate=self.profile.impersonate,
            timeout=self.settings.request_timeout,
            verify=True,
        )
        self.vault.register_refresher(self._refresh_session)
        self.vault.start_watch()
        self._started = True
        logger.info(
            "hybrid requester ready (profile=%s, proxies=%d, bridge=%s)",
            self.profile.name,
            len(self.pool),
            self.bridge.url if self.bridge.is_running else "off",
        )

    async def close(self) -> None:
        await self.vault.stop_watch()
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.close()
            self._http = None
        self.bridge.stop()
        self._started = False

    async def __aenter__(self) -> HybridRequester:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    #  Proxy plumbing
    # ------------------------------------------------------------------
    def _proxies_for(self, endpoint: ProxyEndpoint | None) -> dict[str, str] | None:
        if endpoint is None:
            return None
        return {"http": endpoint.uri, "https": endpoint.uri}

    def _rotate(self, session_id: str) -> ProxyEndpoint | None:
        endpoint = self.pool.rotate(session_id)
        self.stats_rotations += 1
        if self.bridge.is_running:
            self.bridge.set_upstream(endpoint)
        return endpoint

    async def _polite_delay(self) -> None:
        """Jittered spacing between calls — both a bot signal and simple courtesy."""
        elapsed = time.monotonic() - self._last_request_at
        wait = jitter(self.settings.min_request_delay, self.settings.max_request_delay)
        if elapsed < wait:
            await asyncio.sleep(wait - elapsed)
        self._last_request_at = time.monotonic()

    # ------------------------------------------------------------------
    #  Fast path: curl_cffi
    # ------------------------------------------------------------------
    async def get_json(
        self,
        site_path: str,
        *,
        referer: str | None = None,
        page_type: str = "pdp",
        session_name: str = "default",
        max_retries: int | None = None,
    ) -> dict[str, Any]:
        """Fetch a Composer API page as decoded JSON, escalating as needed."""
        if self._http is None:
            raise RequesterError("Requester not started; call await start() first")

        site_path = normalise_path(site_path)
        url = OzonUrls.composer(site_path)
        referer = referer or f"{OZON_ORIGIN}{site_path}"
        retries = max_retries if max_retries is not None else self.settings.max_retries
        last_error = "unknown"

        for attempt in range(retries):
            endpoint = self.pool.acquire(session_name)
            async with self._semaphore:
                await self._polite_delay()
                try:
                    response = await self._http.get(
                        url,
                        headers=self.headers.composer(referer, page_type=page_type),
                        cookies=self.vault.cookie_dict(session_name),
                        proxies=self._proxies_for(endpoint),
                        timeout=self.settings.request_timeout,
                        allow_redirects=False,  # a 307 here means a URL-shape bug
                    )
                except Exception as exc:
                    last_error = f"transport error: {exc}"
                    logger.warning("HTTP attempt %d failed: %s", attempt + 1, exc)
                    self.pool.report_failure(endpoint)
                    self._rotate(session_name)
                    await self._backoff(attempt)
                    continue

            self.stats_http_requests += 1
            body = response.text or ""

            if response.status_code in (301, 302, 307, 308):
                # Almost always the trailing-slash bug; surface it loudly rather
                # than following the redirect back to page 1.
                location = response.headers.get("location", "")
                raise RequesterError(
                    f"Unexpected {response.status_code} redirect for {site_path!r} "
                    f"-> {location!r}. Check for a '/' before '?' in the path."
                )

            verdict = self.detector.inspect(response.status_code, body, response.headers)

            if not verdict.blocked and response.status_code < 400:
                self.pool.report_success(endpoint)
                try:
                    return response.json()
                except Exception as exc:
                    last_error = f"malformed JSON: {exc}"
                    logger.warning("could not decode composer JSON: %s", exc)
                    await self._backoff(attempt)
                    continue

            self.stats_challenges += 1
            last_error = f"{verdict.kind} ({verdict.detail})"
            logger.info(
                "challenge on attempt %d: %s [%s]", attempt + 1, verdict.kind, verdict.detail
            )

            if verdict.kind is ChallengeKind.JS_POW and verdict.pow_challenge is not None:
                solution = await asyncio.to_thread(
                    self.pow_solver.solve, verdict.pow_challenge
                )
                if solution is not None:
                    self.stats_pow_solved += 1
                    logger.info(
                        "solved PoW natively in %d attempts (%.2fs)",
                        solution.attempts, solution.elapsed,
                    )
                    await self._bootstrap_internal(referer, session_name=session_name)
                    continue

            if verdict.needs_rotation:
                self.pool.report_failure(endpoint, verdict.status)
                self._rotate(session_name)

            if verdict.needs_browser or verdict.kind is ChallengeKind.JS_POW:
                await self._bootstrap_internal(referer, session_name=session_name)

            await self._backoff(attempt)

        raise RequesterError(
            f"Exhausted {retries} attempts for {site_path!r}; last error: {last_error}"
        )

    async def _backoff(self, attempt: int) -> None:
        delay = min(30.0, (2**attempt) + random.uniform(0, 1.5))
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    #  Stealth path: Camoufox
    # ------------------------------------------------------------------
    @contextlib.asynccontextmanager
    async def browser_page(self, *, area_id: int | None = None) -> AsyncIterator[Any]:
        """Yield a Camoufox page wired through the bridge, with cookies restored."""
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError as exc:  # pragma: no cover - deployment issue
            raise RequesterError(
                "Camoufox is not installed. `pip install camoufox[geoip]` and run "
                "`python -m camoufox fetch` to download the browser."
            ) from exc

        launch: dict[str, Any] = {
            "headless": self.settings.browser_headless,
            "locale": self.settings.browser_locale,
            "humanize": True,  # non-linear cursor paths; cheap and effective
            "i_know_what_im_doing": True,
        }
        if self.settings.camoufox_executable:
            launch["executable_path"] = self.settings.camoufox_executable

        if self.bridge.is_running:
            # The browser only ever sees a credential-free loopback HTTP proxy;
            # the bridge attaches the SOCKS5 credentials on the way out.
            launch["proxy"] = {"server": self.bridge.url}
            # Let Camoufox derive timezone/locale from the exit IP so the
            # JS-visible geo story matches the residential RU address.
            launch["geoip"] = True

        self.stats_browser_launches += 1
        async with AsyncCamoufox(**launch) as browser:
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            stored = [c.to_playwright() for c in self.vault.cookies("default")]
            if stored:
                with contextlib.suppress(Exception):
                    await context.add_cookies(stored)
            if area_id is not None:
                with contextlib.suppress(Exception):
                    await context.add_cookies(
                        [{
                            "name": "__Secure-ext_xcid",
                            "value": str(area_id),
                            "domain": ".ozon.ru",
                            "path": "/",
                        }]
                    )
            page = await context.new_page()
            page.set_default_navigation_timeout(self.settings.browser_nav_timeout)
            try:
                yield page
            finally:
                with contextlib.suppress(Exception):
                    harvested = await context.cookies()
                    self.vault.update(
                        harvested,
                        user_agent=self.profile.user_agent,
                        profile=self.profile.name,
                        area_id=area_id,
                        proxy=(
                            self.bridge.current_upstream.safe_uri
                            if self.bridge.current_upstream
                            else None
                        ),
                    )
                with contextlib.suppress(Exception):
                    await page.close()

    async def navigate(
        self,
        url: str,
        *,
        area_id: int | None = None,
        capture_substrings: Sequence[str] = ("composer-api.bx",),
        wait_until: str = "domcontentloaded",
        settle_ms: int = 1_500,
    ) -> BrowserCapture:
        """Physically navigate to ``url`` and capture the XHR payloads it triggers.

        Capturing the page's own Composer calls is what makes regional pricing
        exact: we read the same structured JSON the renderer read, already bound
        to the region the navigation established, instead of re-deriving prices
        from rendered HTML.
        """
        import json as _json

        started = time.monotonic()
        captured: list[dict[str, Any]] = []

        async with self.browser_page(area_id=area_id) as page:

            async def on_response(response: Any) -> None:
                if not any(s in response.url for s in capture_substrings):
                    return
                try:
                    text = await response.text()
                    captured.append(_json.loads(text))
                except Exception:
                    pass  # non-JSON or already-consumed body; not fatal

            page.on("response", on_response)
            await page.goto(url, wait_until=wait_until)
            cleared = await self._await_challenge(page)
            if settle_ms:
                await page.wait_for_timeout(settle_ms)

            html = await page.content()
            final_url = page.url
            cookies = await page.context.cookies()

        return BrowserCapture(
            url=url,
            final_url=final_url,
            html=html,
            captured=captured,
            cookies=cookies,
            challenge_cleared=cleared,
            duration=time.monotonic() - started,
        )

    async def _await_challenge(self, page: Any) -> bool:
        """Block until the WAF interstitial clears. Camoufox runs the real PoW."""
        deadline = time.monotonic() + self.settings.browser_challenge_timeout
        cleared = False
        while time.monotonic() < deadline:
            try:
                html = await page.content()
            except Exception:
                await asyncio.sleep(1.0)
                continue
            verdict = self.detector.inspect(200, html)
            if not verdict.blocked:
                return cleared
            cleared = True
            self.stats_challenges += 1
            logger.info("waiting out WAF challenge (%s)…", verdict.kind)
            await asyncio.sleep(1.5)
        logger.warning("challenge did not clear within the timeout")
        return cleared

    # ------------------------------------------------------------------
    #  Bootstrap
    # ------------------------------------------------------------------
    async def bootstrap(
        self,
        sku: str | int | None = None,
        *,
        url: str | None = None,
        area_id: int | None = None,
        session_name: str = "default",
        force: bool = False,
    ) -> BrowserCapture | None:
        """Warm a session: launch the browser, clear the WAF, harvest cookies.

        Skipped when the vault already holds fresh critical cookies, unless
        ``force=True`` — that check is what keeps browser launches rare.
        """
        if not force and not self.vault.needs_refresh(session_name):
            logger.debug("session %r still hot; skipping bootstrap", session_name)
            return None
        target = url or (
            OzonUrls.product_url(sku, area_id) if sku is not None else f"{OZON_ORIGIN}/"
        )
        return await self._bootstrap_internal(
            target, area_id=area_id, session_name=session_name
        )

    async def _bootstrap_internal(
        self,
        url: str,
        *,
        area_id: int | None = None,
        session_name: str = "default",
    ) -> BrowserCapture:
        logger.info("bootstrapping session via stealth browser: %s", url)
        capture = await self.navigate(url, area_id=area_id)
        self.vault.update(
            capture.cookies,
            name=session_name,
            user_agent=self.profile.user_agent,
            profile=self.profile.name,
            area_id=area_id,
        )
        missing = self.vault.missing_critical(session_name)
        if missing:
            logger.warning("bootstrap finished but cookies still missing: %s", missing)
        return capture

    async def _refresh_session(self) -> None:
        """Watchdog callback: re-mint cookies before they expire (spec §3.3)."""
        await self._bootstrap_internal(f"{OZON_ORIGIN}/", session_name="default")

    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        return {
            "started": self._started,
            "profile": self.profile.name,
            "impersonate": self.profile.impersonate,
            "http_requests": self.stats_http_requests,
            "browser_launches": self.stats_browser_launches,
            "challenges_seen": self.stats_challenges,
            "pow_solved_natively": self.stats_pow_solved,
            "proxy_rotations": self.stats_rotations,
            "bridge": {
                "running": self.bridge.is_running,
                "url": self.bridge.url if self.bridge.is_running else None,
                "upstream": (
                    self.bridge.current_upstream.safe_uri
                    if self.bridge.current_upstream
                    else None
                ),
                "dial_successes": self.bridge.dial_successes,
                "dial_failures": self.bridge.dial_failures,
            },
            "proxies": self.pool.stats(),
            "session": self.vault.stats(),
        }
