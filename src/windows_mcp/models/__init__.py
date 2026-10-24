"""Fast model clients used inside the server: the Do agent loop and the Route scorer.

Pure Python (``requests`` only, no Windows imports) so ``scripts/bench_models.py`` runs on
any host. Nothing is sent anywhere until a tool that needs a model is called and its
provider key is set; without one, ``Do``/``Route`` fail with "no model configured".
"""

from windows_mcp.models.config import ModelError, ModelSpec, ModelUnavailable, parse_spec

__all__ = ["ModelError", "ModelSpec", "ModelUnavailable", "parse_spec"]
