"""One-shot OAuth for the Cosmos Decoded channel.

Uses the proven pipeline.upload.authenticate() flow. Stdout is forced
unbuffered (line_buffering=True) so the auth URL prints immediately.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

from pipeline.upload import authenticate

authenticate(account="cosmosdecoded", interactive=True)
print("AUTH OK — token cached.")
