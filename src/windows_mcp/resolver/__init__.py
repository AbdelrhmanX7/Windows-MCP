"""Resolve a click target described in words to a UI element, inside the server.

See :mod:`windows_mcp.resolver.service` for the tiers and safety rules.
"""

from windows_mcp.resolver.scoring import Candidate, Query, parse_target, rank, split_path
from windows_mcp.resolver.service import Resolution, ResolveError, Resolver

__all__ = [
    "Candidate",
    "Query",
    "Resolution",
    "ResolveError",
    "Resolver",
    "parse_target",
    "rank",
    "split_path",
]
