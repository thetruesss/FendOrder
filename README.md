# Ozon Stock / Delivery / PVZ Parser

Async module that reads a product's **total seller stock**, the **FBO fulfilment
split** (packages, per-warehouse counts and exact delivery dates) and the
selected **pickup point (ПВЗ)** for a given Ozon SKU, driving an authorised
session through an anti-detection browser over a residential SOCKS5 exit.

```
ozon_parser/
├── config.py         Settings (proxy, bridge, browser) — all env-overridable
├── proxy_bridge.py   Async HTTP→SOCKS5 bridge (127.0.0.1:8889) via PySocks
├── cookies.py        Cookie loader (Netscape / JSON / header-string)
├── browser.py        AsyncCamoufox launch, pointed at the local bridge
├── defender.py       Ozon Defender challenge resolver (#reload-button)
├── stock.py          "осталось" regex + cart-probe fallback
├── checkout.py       /gocheckout split-packages, dates, warehouses, PVZ
├── parser.py         OzonStockParser — orchestrates the full pipeline
└── models.py         StockResult / Package dataclasses
main.py               CLI entry point
```

## Pipeline

1. **Bridge up.** A local HTTP proxy on `127.0.0.1:8889` terminates the
   browser's `CONNECT` tunnel and re-opens it through the authenticated Soxway
   SOCKS5 exit — Playwright/Camoufox can't pass SOCKS credentials directly, so
   the bridge does the handshake with `socks.socksocket`.
2. **Camoufox up.** Launched with `proxy → bridge`, `geoip=True`, Moscow
   locale/timezone, and the authorised cookies from `cookies.txt`.
3. **Open product** `https://www.ozon.ru/product/{SKU}/`.
4. **Defender resolver.** If the challenge is detected (page title / reload
   button), click `#reload-button` with `force=True` and wait for the
   `?__rr=1&abt_att=1` redirect. `page.reload()` is deliberately **not** used
   (avoids `NS_BINDING_ABORTED`).
5. **Total stock.** Regex the card counter
   (`(\d+[\d\s]*)\s*(?:единиц|единицы|штук|шт)?\s*осталось`). If absent, run the
   **cart probe**: add to cart, request `10000` units and read the
   server-enforced maximum. No static numbers — an illiquid DOM yields `0` and a
   `hydration_error` status.
6. **Checkout split.** From the cart, click *Перейти к оформлению*, land on
   `/gocheckout?activeTab=0&session_uid=…`, locate the `splitPackages` /
   `checkoutSplit` state and extract per-package warehouse, count and delivery
   date plus the selected PVZ address.
7. **Write** `stock_{SKU}.json`.

## Setup

```bash
pip install -r requirements.txt
python -m camoufox fetch          # one-time: download the Camoufox browser
```

Provide an authorised session (see `cookies.example.txt` for the accepted
formats):

```bash
cp cookies.example.txt cookies.txt   # then paste real values
export COOKIES_PATH=./cookies.txt    # default is /app/cookies.txt
```

Credentials default to the values from the spec but can be overridden:

| Variable | Default |
| --- | --- |
| `SOXWAY_HOST` / `SOXWAY_PORT` | `v1.soxway.io` / `1080` |
| `SOXWAY_USER` / `SOXWAY_PASS` | Soxway RU-Moscow session |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `127.0.0.1` / `8889` |
| `COOKIES_PATH` | `/app/cookies.txt` |
| `HEADLESS` | `true` |
| `OUTPUT_DIR` | `.` |

> For anything beyond a local run, move the proxy credentials out of the
> defaults and into the environment / a secret store — do not commit real
> credentials.

## Usage

```bash
python main.py 2160940482
python main.py 2160940482 987654321 --no-headless --log-level DEBUG
```

Programmatic:

```python
import asyncio
from ozon_parser import OzonStockParser

async def main():
    async with OzonStockParser() as parser:
        result = await parser.parse("2160940482")
        parser.save(result)          # -> stock_2160940482.json
        print(result.to_dict())

asyncio.run(main())
```

## Output — `stock_{SKU}.json`

```json
{
  "sku": "2160940482",
  "total_stock": 294,
  "product_title": "...",
  "delivery_pvz": "ПВЗ Москва, ул. Ленина 12",
  "packages_split": [
    { "package_id": 1, "warehouse": "Софьино",   "count": 33, "delivery_date": "5 августа" },
    { "package_id": 2, "warehouse": "Хоругвино", "count": 24, "delivery_date": "6 августа" },
    { "package_id": 3, "warehouse": "Бугры",     "count": 11, "delivery_date": "8 августа" }
  ],
  "status": "ok"
}
```

`status` is one of `ok`, `stock_only` (stock read but no checkout split),
`hydration_error` (no real stock figure — returns `0`, never a stub) or
`challenge_blocked` (Defender not cleared).

## Notes

- Requires Python **3.11+** (async, `dataclass(slots=True)`, PEP 604 unions).
- The checkout state walker (`checkout.py`) is intentionally shape-tolerant:
  Ozon's `splitPackages` payload varies between A/B buckets, so field names are
  matched from candidate sets and missing data is returned empty rather than
  guessed. Selectors and key lists are the first things to adjust against live
  DOM during review.
