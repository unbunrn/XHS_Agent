"""Source-selection policy shared by the Creator Agent runtime.

The legacy fixed topic pipeline has been retired.  This module keeps only the
small, generic helper that reads the configured DeepSeek Search allowlist;
source verification itself lives in ``source_identity``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from bot.adapters.sources import domain_allowlist

if TYPE_CHECKING:
    from bot.config import Config


def trusted_search_domains(config: "Config") -> tuple[str, ...]:
    """Union of configured DeepSeek Search domain allowlists.

    No domain is added here.  The allowlist is an advisory/source-tier hint;
    it is not a hard gate for the Creator Agent.
    """
    domains: list[str] = []
    for entry in config.enabled_sources():
        kind = str(entry.get("kind", "")).lower()
        if kind not in {"deepseek-search", "deepseek_search"}:
            continue
        domains.extend(domain_allowlist(entry.get("allowed_domains")))
    return tuple(dict.fromkeys(domains))
