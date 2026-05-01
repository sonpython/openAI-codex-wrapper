"""
Shared Jinja2 templates environment for the admin UI.

Centralised here so sub-router modules (keys_page_routes.py, tiers_page_routes.py)
can import the singleton without creating circular dependencies on routes.py.

Auto-escape is ON for XSS protection.
"""

from __future__ import annotations

import os

from fastapi.templating import Jinja2Templates

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")

templates = Jinja2Templates(directory=_TEMPLATE_DIR)
templates.env.autoescape = True
