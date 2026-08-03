#!/usr/bin/env python3
"""CLI entry point for the Ozon stock / delivery / PVZ parser.

Usage::

    python main.py 2160940482
    python main.py 2160940482 987654321 --no-headless
    COOKIES_PATH=./cookies.txt python main.py 2160940482

The result of each SKU is printed to stdout and written to ``stock_{sku}.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from ozon_parser import OzonStockParser, load_settings


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse Ozon stock, packages and PVZ for SKUs.")
    parser.add_argument("skus", nargs="+", help="One or more Ozon SKUs to parse.")
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run Camoufox with a visible window (overrides HEADLESS env).",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for stock_{sku}.json (overrides OUTPUT_DIR env).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ...).",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.no_headless:
        settings.browser.headless = False
    if args.output_dir:
        settings.output_dir = args.output_dir

    exit_code = 0
    async with OzonStockParser(settings) as parser:
        for sku in args.skus:
            try:
                result = await parser.parse(sku)
            except Exception:
                logging.exception("Failed to parse SKU %s", sku)
                exit_code = 1
                continue

            parser.save(result)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            if result.status.value not in ("ok", "stock_only"):
                exit_code = 1
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
