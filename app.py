"""Vercel entrypoint: the Python runtime looks for a top-level `app` here."""

from fkl.api import app

__all__ = ["app"]
