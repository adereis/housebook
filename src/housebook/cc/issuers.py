"""Issuer alias resolution for CC sidecars.

The CC `source` column is the issuer name copied from each sidecar's
`data.issuer` field. Sidecars are produced by an AI agent following
`prompts/cc/import.md`, so the same card can be written with slightly
different spellings across statements ("Home-Goods" vs "Home Goods").
Left unnormalized, those variants become *distinct* sources in the DB
and silently fragment one card's history (this happened — see the
`Home Goods`/`Home-Goods` split that motivated this module).

`config/cc/issuers.json` already defines the canonical name and its
aliases for every card; previously only the import SOP consulted it.
This resolver makes the *code* consult it too, so the canonical name
is the single authority for what lands in the DB.

Mirrors `hsa/providers.py::ProviderResolver`, with one deliberate
difference: resolution is **exact-match only** (after normalizing
`-`→space and upcasing). The HSA resolver also does substring
fallback, which is unsafe for issuers — "Chase" is a substring of
"Chase-Amazon" and would wrongly collapse two real cards into one.
An unknown issuer is returned as-is (stripped), never guessed.
"""

from __future__ import annotations

import json
import os

from housebook.config.settings import CC_ISSUERS_JSON


class IssuerResolver:
    def __init__(self, issuers_json: str | None = None):
        self.issuers_json = issuers_json or CC_ISSUERS_JSON
        self._map = self._load()  # normalized alias/name -> canonical

    @staticmethod
    def _norm(name: str) -> str:
        return name.replace("-", " ").strip().upper()

    def _load(self) -> dict:
        """Build a {normalized -> canonical} map from issuers.json."""
        if not os.path.exists(self.issuers_json):
            return {}
        with open(self.issuers_json) as f:
            data = json.load(f)

        mapping: dict[str, str] = {}
        for issuer in data.get("issuers", []):
            canonical = issuer["name"]
            mapping[self._norm(canonical)] = canonical
            for alias in issuer.get("aliases", []):
                mapping[self._norm(alias)] = canonical
        return mapping

    def resolve(self, raw_name: str) -> str:
        """Return the canonical issuer name for `raw_name`.

        Known canonical names and aliases collapse to the canonical
        form. Unknown names are returned stripped but otherwise
        untouched — we never invent a mapping.
        """
        if not raw_name:
            return raw_name
        return self._map.get(self._norm(raw_name), raw_name.strip())

    def is_known(self, raw_name: str) -> bool:
        """True if `raw_name` matches a known canonical name or alias."""
        return bool(raw_name) and self._norm(raw_name) in self._map
