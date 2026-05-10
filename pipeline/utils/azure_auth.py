"""Azure auth helper — single shared API key for all Azure AKS services.

The cloud_azure ingress validates `X-API-Key: <secret>` via nginx
auth-snippet annotation. The secret is generated once during
`cloud_azure/install_addons.sh` and printed by `print_envs.sh` as
`AZURE_API_KEY=...` for the laptop's `.env`.

We deliberately keep this stupid-simple (no Azure AD / OIDC / managed
identity) because:
  * The traffic is laptop → ingress only, never service-to-service.
  * The data on the wire is text prompts and audio bytes — neither is
    high-value enough to warrant Azure AD's ceremony.
  * The API key already gives us cluster-wide auth without managing
    per-service principals.

If the key ever needs rotation: re-run `install_addons.sh` (it skips
existing Secrets but you can `kubectl delete secret azure-api-key` first
to force regeneration), then re-run `print_envs.sh > .env`.
"""
from __future__ import annotations

import os


class AzureKeyMissing(RuntimeError):
    """Raised when AZURE_API_KEY is not set in the environment."""


def get_api_key() -> str:
    key = os.environ.get("AZURE_API_KEY", "").strip()
    if not key:
        raise AzureKeyMissing(
            "AZURE_API_KEY not set. Either source the laptop .env "
            "(after running cloud_azure/print_envs.sh > .env), or "
            "unset the AZURE_*_URL env vars to fall back to GCP."
        )
    return key


def tls_verify() -> bool:
    """Whether to verify TLS against the ingress cert.

    Default True. Set ``AZURE_TLS_VERIFY=0`` when running against a
    self-signed cert on nip.io (the canary default). Once a real
    domain + cert-manager + Let's Encrypt are wired up, leave unset
    so verify is honoured.
    """
    val = os.environ.get("AZURE_TLS_VERIFY", "1").strip().lower()
    return val not in ("0", "false", "no", "off")
