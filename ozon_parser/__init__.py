"""Ozon stock / FBO / delivery-date / PVZ parser.

Public surface::

    from ozon_parser import OzonStockParser, StockResult, load_settings
"""

from __future__ import annotations

from .config import Settings, load_settings
from .models import Package, ParseStatus, StockResult
from .parser import OzonStockParser

__all__ = [
    "OzonStockParser",
    "StockResult",
    "Package",
    "ParseStatus",
    "Settings",
    "load_settings",
]

__version__ = "1.0.0"
