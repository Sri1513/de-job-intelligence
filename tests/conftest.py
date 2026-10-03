# tests/conftest.py
"""Test-session bootstrap.

The MCP app fails closed at import time without MCP_AUTH_TOKEN, so the token
must exist in the environment before any test module imports src.protocols.app.
pytest imports this conftest before collecting test modules, which guarantees
ordering. Production deployments set the real token in .env / environment.
"""
import os

os.environ.setdefault("MCP_AUTH_TOKEN", "test-token")
