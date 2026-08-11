"""Charge l'isolation SQLite avant tout test (pytest importe conftest en premier)."""
import _isolation  # noqa: F401
