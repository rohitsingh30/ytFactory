"""Fire ONE MyStoriesAnimated AITA Short render via the Cloud Run Job.

Sets the required envs, builds a ShortProposal with a self-contained
AITA story in `notes` (so the worker doesn't have to fetch Reddit —
Cloud Run egress to reddit.com is unreliable), and calls
control.core.jobs._enqueue_render_job which:
  1. writes the job doc to Firestore (jobs/<id>)
  2. fires one execution of ytfactory-render-worker-v2 in asia-southeast1

Prints the job_id + execution name. Tail logs with:
  gcloud logging read 'resource.labels.job_name="ytfactory-render-worker-v2"' \
    --project=ytfactory-prod-v3 --freshness=10m
"""
from __future__ import annotations

import os
import sys

# --- env BEFORE importing control.* ----------------------------------------
os.environ["GOOGLE_CLOUD_PROJECT"] = "ytfactory-prod-v3"
os.environ["YTFACTORY_RENDER_BACKEND"] = "cloudrun"
os.environ["YTFACTORY_QUEUE_BACKEND"] = "firestore"
os.environ["YTFACTORY_CLOUDRUN_REGION"] = "asia-southeast1"
os.environ["YTFACTORY_CLOUDRUN_JOB"] = "ytfactory-render-worker-v2"

from control.core.jobs import _enqueue_render_job  # noqa: E402
from control.core.schema import ShortProposal  # noqa: E402

# Self-contained AITA story — the rewrite stage will turn this into
# a hook + 50-80 word narration. Picked a benign cooking-incident
# story so it has clear stakes + a clean closer.
NOTES = """
My (29F) partner (32M) and I have been cooking dinner together for years.
Last night I made a beef stew that took me 5 hours — browning the meat in
batches, deglazing the pan, slow-simmering everything. When I served it,
my partner took two bites and said "it's missing something" and just
poured ketchup on it. The entire plate. I lost it. I told him he'd never
eat anything I cooked again and went to bed. He says I'm being dramatic
and ketchup is a normal condiment. AITA?
""".strip()

proposal = ShortProposal(
    channel="mystoriesanimated",
    format="aita_animated",
    topic="ketchup on slow-cooked beef stew",
    source_kind="user_text",
    source_ref=None,
    length_s=15,  # 2026-05-17 (round 2): cut to 15s for FAST iteration on
                  # the caption + Ken Burns fixes. Cloud cycle ~5-7 min
                  # at 15s instead of ~15 min at 25s.
    notes=NOTES,
    internal_only=True,  # do not auto-publish
)

print("=== firing render ===")
print(f"channel:  {proposal.channel}")
print(f"format:   {proposal.format}")
print(f"length_s: {proposal.length_s}")
print(f"topic:    {proposal.topic}")

resp = _enqueue_render_job(proposal)

print()
print(f"job_id:   {resp.job_id}")
print(f"task_id:  {resp.task_id}")
print()
print("Cloud Run Job execution dispatched. Watch progress with:")
print(f"  gcloud run jobs executions list --job=ytfactory-render-worker-v2 --region=asia-southeast1 --limit=3")
print(f"  python -c \"from google.cloud import firestore; "
      f"c=firestore.Client(project='ytfactory-prod-v3'); "
      f"print(c.collection('jobs').document('{resp.job_id}').get().to_dict())\"")
print(f"  gcloud logging read 'resource.labels.job_name=\"ytfactory-render-worker-v2\" AND "
      f"labels.\"run.googleapis.com/execution_name\":*' --project=ytfactory-prod-v3 --freshness=20m --limit=50")

sys.exit(0)
