#!/bin/bash
# test.sh - Run full test suite and linting

set -e # Exit on error

# Ensure .venv/bin is in PATH if it exists (for non-interactive shells)
if [ -d "./.venv/bin" ]; then
    export PATH="$(pwd)/.venv/bin:$PATH"
fi

# 1. Linting (Static Analysis)
echo "--- RUNNING LINTING ---"

# Check for trailing whitespaces
echo "Checking for trailing whitespaces..."
# Only files git would commit: tracked and untracked-but-not-ignored.
# Ignored generated data (the demo workspace's PDFs, whose xref table
# requires trailing spaces) is not source and must not fail the build.
if git grep -I -n --untracked "[[:space:]]$" -- . ':!input-sources'
then
    echo "❌ Trailing whitespaces found! Please remove them."
    exit 1
fi
echo "✅ No trailing whitespaces."

ruff check .
echo "✅ Ruff linting passed."
echo ""

# 2. Unit Tests
echo "--- RUNNING UNIT TESTS ---"
# Tests must never see the real workspace, even where a test forgets to
# patch a path: give this run a fresh empty one. The export overrides
# the direnv/.env value because load_dotenv() never overwrites.
HOUSEBOOK_WORKSPACE_DIR="$(mktemp -d)"
export HOUSEBOOK_WORKSPACE_DIR
trap 'rm -rf "$HOUSEBOOK_WORKSPACE_DIR"' EXIT
PYTHONPATH=src python3 -m unittest discover tests -b
echo "✅ Unit tests passed."

echo ""
echo "🚀 Codebase is healthy!"
