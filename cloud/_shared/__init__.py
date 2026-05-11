"""Cloud Run shared helpers.

Each ``cloud/<service>/`` directory copies this package into its
container image (see Dockerfile ``COPY cloud/_shared`` lines).
Modules here MUST stay dependency-light — they're loaded by every
single cloud container regardless of which TTS / image / render
service is being built.
"""
