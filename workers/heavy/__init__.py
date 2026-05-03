"""Heavy workers — invoked exclusively by the laptop agent runner.

Importing this module registers all heavy-side TaskKinds with the runner.
Each worker module self-registers via `runner.register(...)` at import time.
The agent's main.py imports this package on startup so the registry is
populated before the lease loop starts.
"""
from __future__ import annotations

from workers.heavy import render_short  # noqa: F401 — registers RENDER_SHORT
