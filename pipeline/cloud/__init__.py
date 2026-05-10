"""ytFactory cloud-side helpers.

Two unrelated concerns share this package:

1. Existing client glue used during a render — :mod:`pipeline.cloud.cloudrun_auth`
   (per-audience ID-token cache) and :mod:`pipeline.cloud.skill_dispatch`
   (website-native render dispatch).
2. The admin-panel core added 2026-05-10 — :mod:`pipeline.cloud.services`,
   :mod:`.health`, :mod:`.cost`, :mod:`.deploys`, :mod:`.warm`,
   :mod:`.snapshot`. These power the Next.js Cloud tab, the daily snapshot
   cron, and the renderer warm hook. Replaces four short-lived
   ``.claude/skills/`` (cloud-cost / cloud-health / deploy-cloud-service /
   warm-cloud) that didn't fit the project's skill taxonomy.

Importing from the top level only re-exports the catalog primitives so
the heavier submodules can stay lazy.
"""

from .services import Service, ServiceKind, get_service, list_services  # noqa: F401
