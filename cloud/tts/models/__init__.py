"""Models package — one module per supported provider.

Each module exposes a ``synth_<name>`` function with the same signature
shape so ``handler.synthesize`` can dispatch uniformly.
"""
