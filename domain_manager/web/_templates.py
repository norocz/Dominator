"""Sdílená instance Jinja2Templates pro všechny route moduly.

cache_size=0: Zabbix-bug workaround — v Jinja2 3.2+ na Pythonu 3.14+
_load_template() vkládá globals dict do cache_key tuple (dict není hashable).
S cache_size=0 se LRU cache přeskočí úplně. Předáváme předkonfigurované
jinja2.Environment aby se vyhnuli deprecated env_options v Starlette 0.46+.
"""
import jinja2
from fastapi.templating import Jinja2Templates
from pathlib import Path

_templates_dir = Path(__file__).parent / "templates"

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_templates_dir)),
    autoescape=True,
    cache_size=0,
)

templates = Jinja2Templates(env=_env)
