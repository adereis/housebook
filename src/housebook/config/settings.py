import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Always points to the git repository root
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def resolve_workspace(value: str | None) -> Path:
    """Return the configured workspace, or exit explaining what to set.

    There is deliberately no default. Falling back to the repo root put
    statements, tax extracts and live config inside the checkout, and a
    missing directory usually means a typo or an unmounted drive that
    would otherwise grow a second, empty ledger.
    """
    if not value or not value.strip():
        raise SystemExit(
            "error: HOUSEBOOK_WORKSPACE_DIR is not set. Point it "
            "(in .env, see .env.example) at a directory outside this "
            "repository that holds, or will hold, your financial data."
        )
    workspace = Path(value.strip()).expanduser().resolve()
    if not workspace.is_dir():
        raise SystemExit(
            f"error: HOUSEBOOK_WORKSPACE_DIR={value.strip()} does "
            "not exist. Create the directory (or fix the path) first."
        )
    return workspace


WORKSPACE_DIR = resolve_workspace(
    os.environ.get("HOUSEBOOK_WORKSPACE_DIR")
)

# External Workspace Paths (Stateful & Sensitive)
DB_PATH = str(WORKSPACE_DIR / "data" / "finance.db")
BACKUP_DIR = str(WORKSPACE_DIR / "data" / "backups")
CONFIG_DIR = WORKSPACE_DIR / "config"
RULES_JSON = str(CONFIG_DIR / "rules.json")
EXCLUSIONS_JSON = str(CONFIG_DIR / "exclusions.json")
HEURISTICS_JSON = str(CONFIG_DIR / "heuristics.json")
RECONCILER_CONFIG_JSON = str(CONFIG_DIR / "reconciler_config.json")
USER_PROFILE_JSON = str(CONFIG_DIR / "user_profile.json")
PROJECT_TYPES_JSON = str(CONFIG_DIR / "project_types.json")

INPUT_DIR = str(WORKSPACE_DIR / "input")
AMAZON_DIR = str(Path(INPUT_DIR) / "Amazon")
STATEMENTS_DIR = str(Path(INPUT_DIR) / "Credit-Card-Statements")
TAX_DOCS_DIR = str(Path(INPUT_DIR) / "Tax-Documents")

# HSA module: per-source subtree under workspace root.
# Classified docs land in `hsa/<YYYY>/`. Module config under `config/hsa/`.
HSA_DIR = str(WORKSPACE_DIR / "hsa")
HSA_CONFIG_DIR = CONFIG_DIR / "hsa"
HSA_PROVIDERS_JSON = str(HSA_CONFIG_DIR / "providers.json")
HSA_PATIENTS_JSON = str(HSA_CONFIG_DIR / "patients.json")
HSA_SCANNER_JSON = str(HSA_CONFIG_DIR / "scanner.json")

# CC statements land in `cc/<YYYY>/`. Module config under `config/cc/`.
CC_CONFIG_DIR = CONFIG_DIR / "cc"
CC_ISSUERS_JSON = str(CC_CONFIG_DIR / "issuers.json")

# Repository Paths (Stateless)
PROMPTS_DIR = str(PROJECT_ROOT / "prompts")
EXAMPLE_RULES_JSON = str(PROJECT_ROOT / "config" / "rules.example.json")

RCLONE_REMOTE = os.getenv(
    "HOUSEBOOK_RCLONE_REMOTE", ""
)
