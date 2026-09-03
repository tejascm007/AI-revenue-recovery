"""Loads codes/.env into os.environ - a side-effect-only import.

Gap fix (2026-09-03): python-dotenv has been a listed dependency since this
project's very first pyproject.toml, but nothing anywhere ever actually
called load_dotenv() - a .env file with real keys in it would have been
silently ignored by every credentialed client, all of which read
os.environ.get(...) as a plain module-level constant at import time.

Importing this module (its body runs once per process, Python caches the
rest) is the fix, done here rather than in each individual entrypoint's
main.py: those entrypoints spawn MCP servers as separate subprocesses (each
a fresh Python process), and every credentialed client module - not just
the ones this session happens to import first - needs .env loaded before
ITS OWN os.environ.get(...) constants are evaluated. Every module in this
project with such a constant imports this one first, so where a given
process's entrypoint happens to sit in the import graph stops mattering.
"""

from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_ENV_PATH)
