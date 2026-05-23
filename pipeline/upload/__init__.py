# Lazy re-export of the public API from the upload submodule.
#
# We use module-level ``__getattr__`` (PEP 562) instead of static
# ``from ... import name`` so that ``unittest.mock.patch.object`` and
# direct ``module.attr = ...`` rebinds on the underlying submodule
# (``pipeline.upload.upload``) take effect via the package binding too.
# A static ``from`` import would capture the function object at import
# time and any later rebind on the submodule would be invisible through
# the package namespace.
#
# This mirrors the same pattern in ``pipeline/images/__init__.py``;
# both packages exist as the result of promoting a flat module to a
# package while keeping legacy callers + tests working unchanged.

from pipeline.upload import upload as _upload_mod


_UPLOAD_PUBLIC = (
    "CONFIG_DIR",
    "SCOPES",
    "SECRETS_ROOT",
    "UploadError",
    "RefreshTokenLost",
    "QuotaExceededError",
    "authenticate",
    "compute_throttled_publish_at",
    "derive_metadata",
    "existing_upload",
    "get_channel_sub_count",
    "inspect_token_status",
    "render_description",
    "set_thumbnail",
    "upload_short",
    "write_upload_record",
    "_token_path",
    "_secret_mount_path",
    "_record_path",
    "_mirror_record_to_gcs",
    "main",
)


def __getattr__(name: str):
    """Resolve attribute on the live submodule each access."""
    if name in _UPLOAD_PUBLIC:
        return getattr(_upload_mod, name)
    raise AttributeError(f"module 'pipeline.upload' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_UPLOAD_PUBLIC))
