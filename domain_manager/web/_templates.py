"""Sdílená instance Jinja2Templates pro všechny route moduly.

Centralizováno zde kvůli cache_size=0 — workaround pro Jinja2 3.2.x bug
na Python 3.14, kde _load_template() vkládá surový globals dict do cache_key
tuple a Python 3.14 odmítá tuple s dictem jako klíč (TypeError: unhashable type).
S cache_size=0 se LRU cache zcela přeskočí.
"""
from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates"),
    cache_size=0,
)
