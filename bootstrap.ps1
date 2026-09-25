# bootstrap.ps1 - Windows PowerShell Bootstrap for Housebook

Write-Host "🚀 Bootstrapping Housebook..." -ForegroundColor Cyan

# 1. Setup Virtual Environment
if (-not (Test-Path ".venv")) {
    python -m venv .venv
    Write-Host "✅ Virtual environment created." -ForegroundColor Green
}

# 2. Install Dependencies
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e '.[dev]'
Write-Host "✅ Dependencies and package installed in editable mode." -ForegroundColor Green

# 3. Initialize Database
# Needs HOUSEBOOK_WORKSPACE_DIR (no default; see .env.example).
# Seeds rules from $WORKSPACE/config/rules.json, else the example.
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "✅ Created .env from .env.example." -ForegroundColor Green
}
& .\.venv\Scripts\python.exe -m housebook.init_db
if ($LASTEXITCODE -eq 0) {
    Write-Host "✅ Database initialized and seeded." -ForegroundColor Green
} else {
    Write-Host "⚠️  Database not initialized (see the error above). Set" -ForegroundColor Yellow
    Write-Host "   HOUSEBOOK_WORKSPACE_DIR in .env, then run: housebook-init-db" -ForegroundColor Yellow
}

# 4. Configure Git Hooks
if (Test-Path ".git") {
    git config core.hooksPath .githooks
    Write-Host "✅ Git hooks configured." -ForegroundColor Green
}

Write-Host "✨ Bootstrap complete! Use 'housebook-ingest' or 'housebook-app' to get started." -ForegroundColor Cyan
Write-Host "To activate the environment, run: .\.venv\Scripts\Activate.ps1" -ForegroundColor Yellow
