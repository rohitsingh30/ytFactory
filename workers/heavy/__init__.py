"""Heavy workers — invoked exclusively by the laptop agent runner.

Importing this module is intended to register all heavy-side TaskKinds with
the runner. Each worker module self-registers via `runner.register(...)` at
import time. As workers land, add their imports here.
"""
from __future__ import annotations

# render_short worker lands in the next commit (task #8 in progress).
