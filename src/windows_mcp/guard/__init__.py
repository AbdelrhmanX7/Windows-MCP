"""Scam guard: local lookalike/phishing detector, optional model second opinion, watcher.

``detector`` / ``domains`` / ``confusables`` / ``model`` are pure Python (importable on any OS);
``context`` and ``watcher`` touch UI Automation and import Windows modules lazily.
"""

from windows_mcp.guard.detector import Verdict, check, merge_model, scan_text, warm

__all__ = ["Verdict", "check", "merge_model", "scan_text", "warm"]
