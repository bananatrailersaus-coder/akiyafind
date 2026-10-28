"""Imported first by every test: these tests DROP and recreate tables, so they
must only ever run against a local throwaway database."""
import os, sys
from urllib.parse import urlparse

os.environ.setdefault("DATABASE_URL", "postgresql://akiya:akiya@localhost/akiya")
_host = urlparse(os.environ["DATABASE_URL"]).hostname
if _host not in ("localhost", "127.0.0.1"):
    sys.exit(f"Refusing to run: DATABASE_URL points at {_host}, not a local test database.")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
