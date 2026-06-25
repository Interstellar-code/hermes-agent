"""matrix_coder.core — pure-Python building blocks for the Matrix Coder plugin.

This package holds the framework-agnostic pieces of the plugin: typed models
(:mod:`.models`), configuration defaults (:mod:`.config`), persona discovery
(:mod:`.registry`), persona composition (:mod:`.prompts`), the Hermes
adapter / per-dispatch state (:mod:`.hermes_bridge`), the walking-skeleton
harness (:mod:`.harness`), and output rendering (:mod:`.reporting`).

Nothing here imports the Hermes runtime; the thin plugin entrypoint in
``matrix_coder/__init__.py`` is the only seam that touches ``ctx`` / hooks.
"""

from __future__ import annotations
