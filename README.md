# Ozon B2B Engine v2.0

Anti-bot scraping core and seller analytics platform for the Ozon marketplace,
built to the v2.0 technical specification: review harvesting, region-accurate
pricing and SPP, SERP/GEO tracking, stock estimation, negative-review clustering
and dynamic repricing, behind a FastAPI REST layer.

```
ozon_anti_bot_engine/
├── core/
│   ├── anti_bot.py        TLS profiles, header synthesis, WAF challenge solver
│   ├── proxy_manager.py   HTTP-to-SOCKS5 bridge daemon, pool rotation & health
│   ├── session_vault.py   Cookie store (Netscape + JSON), expiry watchdog
│   └── requester.py       Hybrid requester (Camoufox stealth + curl_cffi HTTP)
├── scrapers/
│   ├── review_scraper.py  Module A — multi-pass reviews, media, points
│   ├── price_scraper.py   Module B — 8 regions, SPP, green price, best-price
│   ├── search_scraper.py  Module C — SERP/GEO, SKU variants, rich content
│   └── stock_scraper.py   Module D — FBO/FBS stock, sales velocity
├── services/
│   ├── sentiment_ai.py    Negative-reason clustering + LLM reply dataset
│   └── repricer.py        Strategies, unit-economics floor, Seller API payload
├── api/endpoints.py       FastAPI REST surface
├── config.py              pydantic-settings configuration + region registry
├── models.py              Shared Pydantic v2 schemas
├── tasks.py               Celery tasks for long-running scrapes
└── main.py                Uvicorn / FastAPI entry point
```

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m camoufox fetch            # download the Gecko build (~150 MB)

cp .env.example .env                # then fill in OZON_PROXY_POOL at minimum
uvicorn ozon_anti_bot_engine.main:app --reload
```

Or with Docker (Camoufox baked into the image):

```bash
cp .env.example .env
docker compose up --build
```

Interactive API docs: <http://localhost:8000/docs>

```bash
pytest                              # 259 tests, no network required
```

---

## Architecture

### The hybrid transport

Two transports share one session, which is where the ~4× proxy-traffic saving
comes from:

| | Camoufox (Gecko) | curl_cffi |
|---|---|---|
| Used for | first touch on a card, clearing the WAF, region navigation | bulk Composer API pagination |
| Fingerprint | C++-level patching inside Gecko — no JS injection to detect | Chrome 124 TLS ClientHello (JA3/JA4) |
| Cost | ~1–3 s + full page assets | ~100 ms, JSON only |

`HybridRequester.get_json()` runs on the fast path and escalates automatically:
a challenge is detected → the browser re-mints cookies → the request resumes. No
caller has to know which transport served it.

### Why the proxy bridge exists

Firefox/Gecko cannot authenticate to a SOCKS5 proxy from a URI — there is no
`network.proxy.socks_username`, and the `socks5://user:pass@host` form is
silently dropped. Commercial residential pools are almost always user/password
authenticated, so the browser half of the engine could not otherwise use the
same exits as the HTTP half.

`HttpToSocks5Bridge` is a threaded loopback HTTP proxy that accepts `CONNECT`
and absolute-form requests with **no** authentication and relays them through
the authenticated SOCKS5 upstream via PySocks. The browser sees a plain,
credential-free proxy; the exit IP is the residential one. The upstream can be
swapped at runtime, so rotation does not restart the listener.

`tests/test_proxy_bridge.py` proves this end to end against a real SOCKS5 server
that rejects unauthenticated handshakes.

### Session lifecycle

`SessionVault` persists cookies in Netscape `cookies.txt` and/or JSON (0600),
tracks time-to-expiry on `__Secure-ETC` / `xcid`, and runs a background watcher
that triggers a browser re-bootstrap **5 minutes before** the earliest expiry —
so a 500-page sweep never dies mid-pagination.

---

## The two rules that decide whether the data is correct

**1. No `/` before `?` on review pagination.**

```
/product/<SKU>/reviews/?sort=score_asc&page=2   →  307 redirect back to page 1
/product/<SKU>/reviews?sort=score_asc&page=2    →  correct page 2
```

The redirect is silent. A scraper that gets this wrong collects page 1 five
hundred times and reports a healthy-looking run. Every URL in the package is
built through `OzonUrls` / `normalise_path()`, and the fast path sets
`allow_redirects=False` so a stray 3xx raises instead of quietly resolving.

**2. Regional prices require physical navigation.**

Swapping the `areaId` cookie leaves a 7–14 ₽ error, because Ozon binds the
logistics component to the region the session was established in. The only
accurate method is a full navigation per region:

```
https://www.ozon.ru/product/<SKU>/?areaId=<ID>
```

`PriceScraper` therefore stays on the browser transport and reads prices from
the Composer payloads the page itself fetched (captured via a response hook),
falling back to the state embedded in the HTML.

| Region | Macro-region | areaId |
|---|---|---|
| Москва | Центр | 2 |
| Санкт-Петербург | Северо-Запад | 3 |
| Краснодар | Юг | 14 |
| Казань | Поволжье | 16 |
| Екатеринбург | Урал | 58 |
| Новосибирск | Сибирь | 67 |
| Хабаровск | Дальний Восток | 79 |
| Архангельск | Север | 2495 |

---

## Modules

### A — Reviews

Ozon caps pagination below the review count on large cards, so one sorted walk
can never reach 100%. Three passes attack the list from different ends and are
unioned by review UUID:

1. `score_desc` — the positive tail (4–5★)
2. `score_asc` — the negative head (1–3★), which the default order buries
3. `published_at_desc` — anything that landed while the first two paginated

Depth is sized from the card's own review count (60% of full depth per pass,
since the union closes the gap), capped by `OZON_REVIEW_MAX_PAGES`. Termination
is on *no new UUIDs for 3 pages*, not on an empty body — Ozon repeats the last
page indefinitely past the real end.

Per review: dignity / shortcoming / comment split, media upgraded from CDN
thumbnails to original quality, seller answers, `pointsForReview`, usefulness
counters, and a written-vs-bare-rating split.

### B — Pricing

Base (struck-through) price, showcase price, Ozon-card «green» price, SPP in ₽
and %, seller discount %, and the «Лучшая цена» badge — per region, with the
cross-region spread exposed as `price_spread_rub`.

### C — Search

SERP position with paid placements counted separately from organic ones (a card
at slot 4 behind three ads is organic #1, not #4). Ad detection covers all three
markings Ozon uses: a boolean flag, `trackingInfo.type`, and a rendered
«Реклама» label. Plus SKU variant groups (склейки), rich content JSON/HTML and
full-resolution media.

### D — Stock

Passive read of the quantity-picker limit and «осталось N шт» label by default;
an opt-in cart probe (`use_cart_probe=true`) for cards that hide it. Velocity is
the negative first derivative of the stock series, with increases reclassified
as restock events rather than negative sales.

### Sentiment clustering

Lexical, not neural — deliberately. The four labels are defined by a small,
stable Russian vocabulary, and a rule engine is auditable, instant, free, and
cannot silently reclassify a category after a model update. Russian morphology
is handled by root matching (`слома` covers `сломан`/`сломался`/`сломанный`).

Labels: `defect_or_damage`, `packaging`, `size_or_description_mismatch`,
`ozon_logistics`, `other`. The service also emits a cleaned JSON payload with a
per-category reply playbook, ready to hand to Claude/Gemini/OpenAI — reviews the
seller already answered are excluded, and only the buyer's first name travels.

### Repricer

`undercut_lowest` / `match_lowest` / `match_average`, with `min_allowed_price`
enforced **last**, after every other calculation, so no future strategy can
route around the floor. Undercut rounding always rounds *down* — rounding up
could land back above the competitor and defeat the undercut. Emits a
`/v1/product/import/prices` payload with string-typed prices and `min_price`
forwarded so Ozon's own auto-actions cannot discount below cost either.

---

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/health` | Engine, bridge and proxy state |
| GET | `/api/v1/health/proxies` | Actively probe every proxy |
| GET | `/api/v1/reviews/{sku}` | Multi-pass review scrape |
| GET | `/api/v1/pricing/{sku}` | Regional prices + SPP |
| GET | `/api/v1/search/position` | SERP position by keyword and region |
| GET | `/api/v1/search/rich-content/{sku}` | Rich content + SKU variants |
| GET | `/api/v1/inventory/{sku}` | Stock estimate + velocity |
| POST | `/api/v1/services/sentiment/cluster` | Cluster negatives, build LLM payload |
| POST | `/api/v1/services/repricer/calculate` | Repricing decisions + payloads |
| GET | `/api/v1/regions` | The 8 tracked macro-regions |

Set `OZON_API_KEY` to require an `X-API-Key` header; leave it empty only when
the service is bound to a private network.

A full review sweep runs for minutes — longer than most HTTP clients will hold
a connection. At scale, submit through the Celery tasks in `tasks.py` and poll:

```bash
celery -A ozon_anti_bot_engine.tasks worker --loglevel=info --concurrency=2
```

---

## Configuration

Every value is read from the environment through `pydantic-settings`; there is
no hardcoded credential anywhere in the tree, and `tests/test_security_hygiene.py`
enforces that mechanically on every run. See `.env.example` for the full
surface. The minimum for real workloads is `OZON_PROXY_POOL` — residential RU
exits targeted at Moscow / Moscow oblast. Without it the engine runs direct and
logs a warning; a datacenter IP is rate-limited quickly.

Politeness is on by default: `OZON_MIN_REQUEST_DELAY` / `OZON_MAX_REQUEST_DELAY`
jitter every call, and concurrency is capped at `OZON_HTTP_CONCURRENCY`.
Constant-interval traffic is both a cheap bot signal and discourteous to the
origin.

---

## Known limits

Worth stating plainly rather than discovering in production:

- **"100% WAF bypass" is not something code can guarantee.** Fabrique is updated
  adversarially. What the engine guarantees is *detection*: every challenge is
  classified into a typed verdict (`js_pow` / `interstitial` / `captcha` /
  `rate_limit` / `forbidden`) and escalated to a real Gecko engine rather than
  silently returning garbage. Native PoW solving handles the hashcash variant;
  anything else is handed to the browser, which runs the real script.
- **Widget schemas are Ozon's, and they change.** The parsers match by widget
  prefix and search by key name rather than pinning exact paths, and every
  extraction point is centralised (`extract_review_dicts`, `_build_regional_price`,
  `extract_serp_items`) so a layout change is a small, local edit. Expect to
  make one occasionally.
- **The parsers were validated against synthetic payloads, not live traffic.**
  This environment has no Ozon access or residential proxies, so field-name
  coverage reflects the documented/observed shapes rather than a live capture.
  The URL rules, region table, transport, bridge, clustering and repricer logic
  are all covered by tests that do not need the network.
- **Sales velocity is an estimate.** Ozon publishes no stock figure; the cart
  and picker methods bound it, and restocks make sales inside an interval
  unobservable. Confidence is reported (`low`/`medium`/`high`) — use it.
- **Best-selling variant is inferred from review count**, the only public signal
  monotone with sales. It is an estimate, not a measurement.
- **Scraping Ozon is against its terms of service**, whatever the technical
  merits, and residential proxy use has its own rules. That is a commercial and
  legal call for the operator, not a technical one — the engine defaults to
  polite pacing and mutates nothing unless you opt into the cart probe.

---

## Tests

```bash
pytest                    # 259 tests, ~6 s, no network
pytest tests/test_proxy_bridge.py -v    # end-to-end SOCKS5 bridge
```

Coverage maps to the spec's acceptance checklist (§8):

| Checklist item | Tests |
|---|---|
| HTTP-to-SOCKS5 bridge for Firefox proxy auth | `test_proxy_bridge.py`, `test_proxy_pool.py` |
| No `/` before `?` in review pagination | `test_url_rules.py` |
| Full `?areaId=` navigation across 8 regions | `test_url_rules.py`, `test_pricing.py` |
| Positive/negative split, media, «Баллы за отзывы» | `test_review_parsing.py` |
| Negative clustering + repricing algorithm | `test_sentiment.py`, `test_repricer.py` |
| No credentials in source | `test_security_hygiene.py` |
