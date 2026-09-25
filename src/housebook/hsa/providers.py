import json
import os

from housebook.config.settings import HSA_PROVIDERS_JSON


class ProviderResolver:
    def __init__(self, providers_json: str = None):
        self.providers_json = providers_json or HSA_PROVIDERS_JSON
        self._provider_configs: dict[str, dict] = {}
        self._providers = self._load_providers()

    def _load_providers(self) -> dict:
        """Load provider alias mapping from config."""
        if not os.path.exists(self.providers_json):
            return {}
        with open(self.providers_json) as f:
            data = json.load(f)

        mapping = {}
        for p in data.get("providers", []):
            canonical = p["canonical_name"]
            self._provider_configs[canonical] = dict(p)
            for alias in p.get("aliases", []):
                key = alias.replace("-", " ").strip().upper()
                mapping[key] = canonical
            key = canonical.replace("-", " ").strip().upper()
            mapping[key] = canonical
        return mapping

    def resolve(self, raw_name: str) -> str:
        """Map a raw provider name to its canonical form."""
        if not raw_name:
            return "UNKNOWN"
        normalized = raw_name.replace("-", " ").strip()
        upper = normalized.upper()
        if upper in self._providers:
            return self._providers[upper]
        best_match = None
        best_len = 0
        for alias, canonical in self._providers.items():
            if alias in upper or upper in alias:
                if len(alias) > best_len:
                    best_match = canonical
                    best_len = len(alias)
        return best_match or normalized

    def get_config(self, canonical_name: str) -> dict:
        """Return cached config for a canonical provider name."""
        return self._provider_configs.get(canonical_name, {})
