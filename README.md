# Housebook: AI-Powered Personal Finance

Housebook is a proactive, local-first financial assistant. It evolves the traditional expense tracker into an intelligent system that discovers life events, learns from user behavior, and maintains a clean separation between raw data and categorization intelligence.

## 🌟 Key Features
- **Hybrid Audit Loop**: All new data is imported into a "Pending Review" state. Your AI coding agent (e.g. Claude Code or Gemini CLI) or the Dashboard is used to refine and verify transactions.
- **Proactive Life Events**: Automatically clusters travel, dining, and transit into "Trips" while surgically excluding routine local spending.
- **Continuous Learning**: User corrections are saved as optimized keywords in the database and periodically backed up to `config/rules.json`.
- **Privacy First**: The code and dashboard run locally, and the ledger lives in a workspace outside the repository. The code makes no LLM API calls. The AI agent that imports and audits documents does read them, so their contents reach whichever LLM provider runs that agent. The optional rclone sync talks only to the storage remote you configure.

## 🛠 Setup & Bootstrapping
1. **System Requirements**: Ensure `poppler-utils` (for `pdftotext`), `sqlite3`, and `direnv` (recommended) are installed.
2. **Workspace**: Create a directory **outside this repository** for your financial data, and set `HOUSEBOOK_WORKSPACE_DIR` to it in `.env` (copy `.env.example`). There is no default. Every command refuses to run until the workspace is set and exists.
3. **Bootstrap**: Run the automated setup script to create the virtual environment and initialize the database:
   ```bash
   ./bootstrap.sh
   ```
4. **Shell Integration**: If using `direnv`, allow it once:
   ```bash
   direnv allow
   ```

## 🚀 Workflow
1. **Check coverage**: See which statements are already ingested and spot gaps:
   ```bash
   housebook-ingest list --latest
   ```
2. **Import & Ingest**: Import new statements via the per-source CLIs:
   ```bash
   housebook-amazon import ~/Downloads/Amazon-Data-Export.zip --profile <name>
   housebook-amazon ingest
   housebook-cc ingest          # After PDF import + sidecar creation
   ```
3. **Audit** (mandatory): Run the monthly audit SOP with the AI Agent to verify and categorize all `UNVERIFIED` transactions. See `prompts/monthly_audit.md`.
4. **Dashboard**: Launch the live interactive UI (FastAPI + Jinja2 + Vue.js):
   ```bash
   housebook-app          # http://127.0.0.1:8000
   ```
   A normal local launch has authentication disabled and is deliberately
   limited to loopback. The launcher rejects non-loopback binds. The app
   also has a fail-closed authenticated-proxy mode for the planned LAN
   deployment, but setting its environment variables alone does not
   publish a supported service: keep Uvicorn on loopback until the TLS
   proxy and the remaining release gates in
   `docs/architecture/tax-security-roadmap.md` are configured and
   verified.
   The repository-side Google OAuth/Caddy profile and its live rollout
   checklist are in [`deploy/lan/README.md`](deploy/lan/README.md).
   That profile can preserve account-free access at
   `http://localhost:8000` on the server itself while requiring Google
   authentication for every request through the LAN hostname.
   Dashboard CSS and JavaScript are packaged locally, so routine use
   makes no CDN or font-service requests and works without internet
   access.

## 🧪 Demo Environment
For evaluation and development without private data, the project includes a high-fidelity demo generator:
```bash
# Generate 'The Ledger Family' demo workspace
housebook-demo-seed

# Launch the app using the demo data
HOUSEBOOK_WORKSPACE_DIR=demo-workspace housebook-app
```
The seeder is idempotent and skips generation when the demo database
already exists.
See `demo-workspace/README.md` for the full "Ledger Family" story.

## 📂 Project Structure
- `src/housebook/`:
    - `core/`: Shared database, reconciliation, spending, and matching logic.
    - `cc/`, `amazon/`, `tax/`, `hsa/`: Isolated source modules and CLIs.
    - `config/`: System settings and internal logic.
    - `templates/`: Dynamic Jinja2 templates for the dashboard.
- `config/`: Example templates (`.example.json`) for rules and user profile. Active config lives in the external workspace.
- `tests/`: Comprehensive unit test suite.
- `pyproject.toml`: Modern Python project configuration and dependencies.

All raw financial data (PDFs, CSVs) and active configuration live outside the repo in `$HOUSEBOOK_WORKSPACE_DIR`. See `AGENTS.md` for the Stateless Repo Architecture details.

## License
MIT. See [`LICENSE`](LICENSE). To report a vulnerability, see [`SECURITY.md`](SECURITY.md).
