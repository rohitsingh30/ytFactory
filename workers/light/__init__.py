"""Light workers — eventually run as Cloud Run jobs.

For v1 these run on the laptop agent (it leases any task whose kind is in
its caps list, light or heavy). When we move them to Cloud Run, the
agent's caps will drop the light kinds and Cloud Run will pick them up.

Importing this package registers the light worker functions with the
runner's registry. Both agent/main.py (v1) and the future Cloud Run
worker entrypoint will import it.
"""
from __future__ import annotations

from workers.light import youtube_upload  # noqa: F401 — registers YOUTUBE_UPLOAD
from workers.light import research_handoff  # noqa: F401 — registers RESEARCH_HANDOFF
