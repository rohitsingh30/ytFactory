"""Source adapters — one per niche.

Each adapter mines raw text from somewhere on the internet and emits
``RawStory`` records into ``data/intermediate/<channel>/raw/``. The
``/make-script`` skill then turns each raw story into a hook-first
``Script`` (see ``pipeline.rewrite``) ready for stages 4-7.
"""

from .base import RawStory, save_raw, load_raw, slugify

__all__ = ["RawStory", "save_raw", "load_raw", "slugify"]
