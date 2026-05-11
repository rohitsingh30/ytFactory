# Minimal ytFactory Makefile — entry points for the laptop side of the
# critique-chat loop and other developer-mode scripts that don't
# warrant their own CLI.
#
# To add a new target keep it terse + self-documenting via `make help`.

.PHONY: help critique-runner critique-once critique-runner-tests test

help:
	@echo "Available targets:"
	@echo "  critique-runner      Start the laptop critique-runner daemon"
	@echo "                       (long-poll Firestore, drive Claude/Copilot,"
	@echo "                       run hard gates, push direct to main on green)."
	@echo "  critique-once        Same daemon but exits after processing the"
	@echo "                       first queued critique. Smoke-test mode."
	@echo "  critique-runner-tests"
	@echo "                       Run the critique-runner test suite."
	@echo "  test                 Run the full pytest suite."

# Start the long-running critique-runner daemon. Works against the
# production Firestore project (ytfactory-prod-v2) so the chat panel
# on the live website can talk to your laptop. Requires:
#   - clean working tree (the gate harness needs an isolated diff)
#   - claude or copilot CLI on PATH (the runner spawns whichever the
#     critique doc selected)
#   - gcloud ADC pointing at ytfactory-prod-v2 (for Firestore reads).
critique-runner:
	@echo "[critique-runner] starting daemon — Ctrl-C to stop"
	@python scripts/critique_runner.py

critique-once:
	@echo "[critique-runner] one-shot mode — process one critique then exit"
	@python scripts/critique_runner.py --once

critique-runner-tests:
	@python -m pytest tests/test_critique_runner.py tests/test_critique_gates.py tests/test_critique_routes.py -v

test:
	@python -m pytest -q
