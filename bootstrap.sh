#!/bin/bash
echo "🚀 Bootstrapping Housebook..."

# 1. Setup Virtual Environment (recreate if broken)
if [ -d ".venv" ] && ! ./.venv/bin/pip --version &>/dev/null; then
    echo "⚠️  Stale venv detected, recreating..."
    rm -rf .venv
fi
if [ ! -d ".venv" ]; then
    python3 -m venv --system-site-packages .venv
    echo "✅ Virtual environment created."
fi

# 2. Install Dependencies
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -e '.[dev]'
echo "✅ Dependencies and package installed in editable mode."

# 3. Initialize Database
# Needs HOUSEBOOK_WORKSPACE_DIR (no default; see .env.example).
# Seeds rules from $WORKSPACE/config/rules.json, else the example.
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "✅ Created .env from .env.example."
fi
if PYTHONPATH=src ./.venv/bin/python3 -m housebook.init_db; then
    echo "✅ Database initialized and seeded."
else
    echo "⚠️  Database not initialized (see the error above). Set"
    echo "   HOUSEBOOK_WORKSPACE_DIR in .env, then run: housebook-init-db"
fi

if command -v direnv &> /dev/null; then
    direnv allow
    echo "✅ direnv authorized."
fi

# 4. Configure Git Hooks
if [ -d ".git" ]; then
    git config core.hooksPath .githooks
    echo "✅ Git hooks configured."
fi

echo "✨ Bootstrap complete! Use 'housebook-ingest' or 'housebook-app' to get started."
