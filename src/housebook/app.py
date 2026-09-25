import datetime
import ipaddress
import json
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from housebook.config.settings import (
    BACKUP_DIR,
    DB_PATH,
    RULES_JSON,
    WORKSPACE_DIR,
)
from housebook.core.dashboard import (
    DashboardQueryError,
    load_spending_dashboard,
    load_tax_documents,
)
from housebook.core.database import Database, backup_database
from housebook.core.knowledge_manager import clean_keyword
from housebook.core.models import (
    CATEGORY_CC_PAYMENT,
    CATEGORY_TRANSFERS_REFUNDS,
)
from housebook.core.spending import spend_filter
from housebook.migrations.runner import get_schema_version, run_migrations

# The spending-view predicate, aliased for the `transactions t` joins
# used throughout this file. One definition, seven former copies.
SPEND_FILTER_T = spend_filter("t")

_AUTH_DISABLED = "disabled"
_AUTH_PROXY = "proxy"
_SUPPORTED_AUTH_MODES = {_AUTH_DISABLED, _AUTH_PROXY}
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


@asynccontextmanager
async def lifespan(app):
    # Auto-apply pending schema migrations on startup
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    backup_database(DB_PATH, BACKUP_DIR)
    run_migrations(DB_PATH, verbose=True)
    yield


def _allowed_hosts() -> list[str]:
    """Return the explicit Host allowlist for the UI."""
    configured = os.getenv("HOUSEBOOK_ALLOWED_HOSTS", "")
    if configured:
        hosts = [host.strip() for host in configured.split(",") if host.strip()]
        if hosts:
            return hosts
    # ``testserver`` is Starlette's in-process TestClient host. The other
    # entries are the loopback names used by the default uvicorn binding.
    return ["127.0.0.1", "localhost", "[::1]", "::1", "testserver"]


def _csv_env(name: str) -> set[str]:
    """Return trimmed, non-empty values from a comma-delimited setting."""
    return {
        value.strip()
        for value in os.getenv(name, "").split(",")
        if value.strip()
    }


def _auth_configuration_error() -> str | None:
    """Describe an invalid authentication configuration, if any."""
    mode = os.getenv("HOUSEBOOK_AUTH_MODE", _AUTH_DISABLED).strip()
    if mode not in _SUPPORTED_AUTH_MODES:
        return "unsupported authentication mode"
    if mode == _AUTH_DISABLED:
        return None

    proxy_secret = os.getenv("HOUSEBOOK_PROXY_SECRET", "")
    if len(proxy_secret) < 32:
        return "proxy authentication is not fully configured"
    local_bypass = os.getenv(
        "HOUSEBOOK_ALLOW_LOCAL_BYPASS", "",
    ).strip().lower()
    if local_bypass not in _TRUE_VALUES | _FALSE_VALUES:
        return "local authentication bypass setting is invalid"
    return None


def _is_loopback_host(host: str) -> bool:
    """Return whether Uvicorn may bind directly to this host."""
    return host.strip().lower() in _LOOPBACK_HOSTS


def _local_bypass_enabled() -> bool:
    """Return whether explicit unauthenticated loopback access is enabled."""
    value = os.getenv(
        "HOUSEBOOK_ALLOW_LOCAL_BYPASS", "",
    ).strip().lower()
    return value in _TRUE_VALUES


def _is_direct_loopback_request(request: Request) -> bool:
    """Require both the transport peer and requested host to be loopback."""
    if request.client is None or request.url.hostname is None:
        return False
    try:
        peer_is_loopback = ipaddress.ip_address(
            request.client.host,
        ).is_loopback
    except ValueError:
        return False
    return peer_is_loopback and _is_loopback_host(request.url.hostname)


def _origin_error(request: Request) -> JSONResponse | None:
    """Validate the exact browser Origin for state-changing requests."""
    if request.method not in _UNSAFE_METHODS:
        return None
    allowed_origins = _csv_env("HOUSEBOOK_ALLOWED_ORIGINS")
    if not allowed_origins:
        return JSONResponse(
            status_code=503,
            content={"detail": "allowed origins are not configured"},
        )
    if request.headers.get("origin") not in allowed_origins:
        return JSONResponse(
            status_code=403,
            content={"detail": "request origin is not allowed"},
        )
    return None


def _cross_site_error(request: Request) -> JSONResponse | None:
    """Refuse browser writes from other origins when auth is disabled.

    Proxy mode checks an exact Origin allowlist instead. Here the only
    legitimate browser writer is the dashboard itself, so an unsafe
    request that a browser marks as coming from anywhere else (Origin,
    or Sec-Fetch-Site when Origin is absent) is refused. Clients that
    send neither, such as curl, are not browsers a hostile page can
    drive. Before this, CSRF safety rested implicitly on every POST
    taking a JSON body that a cross-site form cannot produce.
    """
    if request.method not in _UNSAFE_METHODS:
        return None
    own_origin = f"{request.url.scheme}://{request.url.netloc}"
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site")
    if (origin is not None and origin != own_origin) or (
        fetch_site not in (None, "same-origin", "none")
    ):
        return JSONResponse(
            status_code=403,
            content={"detail": "cross-site request refused"},
        )
    return None


app = FastAPI(lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts())
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Setup templates directory
PACKAGE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(PACKAGE_DIR, "templates"))

# Help drawer topics, in order: (anchor, label). base.html includes the
# drawer on every page and opens it at `help_topic`, which defaults to
# the page's `active_module`, so a module's anchor must equal its name.
HELP_SECTIONS = [
    ("overview", "Getting started"),
    ("spending", "Spending"),
    ("trips", "Trips"),
    ("manual", "Manual expenses"),
    ("projects", "Projects"),
    ("tax", "Income & Tax"),
    ("hsa", "HSA Shoebox"),
    ("review", "Review status"),
    ("privacy", "Your data"),
]
templates.env.globals["HELP_SECTIONS"] = HELP_SECTIONS
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(PACKAGE_DIR, "static")),
    name="static",
)


@app.middleware("http")
async def require_authenticated_proxy(request: Request, call_next):
    """Enforce the reverse proxy contract before any route is served."""
    mode = os.getenv("HOUSEBOOK_AUTH_MODE", _AUTH_DISABLED).strip()
    configuration_error = _auth_configuration_error()
    if configuration_error:
        return JSONResponse(
            status_code=503,
            content={"detail": configuration_error},
        )

    if mode == _AUTH_DISABLED:
        if cross_site_error := _cross_site_error(request):
            return cross_site_error
        request.state.authenticated_email = "local-user"
        request.state.authentication_path = "local-disabled"
        return await call_next(request)

    local_bypass = (
        _local_bypass_enabled()
        and _is_direct_loopback_request(request)
    )
    if local_bypass:
        request.state.authenticated_email = "local-user"
        request.state.authentication_path = "local-bypass"
    else:
        expected_secret = os.environ["HOUSEBOOK_PROXY_SECRET"]
        provided_secret = request.headers.get(
            "x-housebook-proxy-secret", "",
        )
        if not secrets.compare_digest(
            provided_secret.encode("utf-8"),
            expected_secret.encode("utf-8"),
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "authentication required"},
            )

        email = request.headers.get(
            "x-auth-request-email", "",
        ).strip().lower()
        if len(email) > 254 or not _EMAIL_PATTERN.fullmatch(email):
            return JSONResponse(
                status_code=401,
                content={"detail": "authentication required"},
            )
        request.state.authenticated_email = email
        request.state.authentication_path = "proxy"

    if origin_error := _origin_error(request):
        return origin_error
    return await call_next(request)


@app.middleware("http")
async def add_no_cache_header(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


db = Database(DB_PATH, workspace_dir=str(WORKSPACE_DIR))


class CategoryUpdate(BaseModel):
    category: str

    @field_validator("category")
    @classmethod
    def must_not_be_system_category(cls, v: str) -> str:
        if v in (CATEGORY_TRANSFERS_REFUNDS, CATEGORY_CC_PAYMENT):
            raise ValueError(
                f"'{v}' is a system-managed category and cannot "
                f"be assigned manually"
            )
        return v


class ReviewToggle(BaseModel):
    needs_review: bool


class TripUpdate(BaseModel):
    trip_id: int | None


class TripCreate(BaseModel):
    name: str
    start_date: str
    end_date: str
    status: str = "confirmed"
    type: str = "unknown"
    location: str | None = None


class ManualExpenseCreate(BaseModel):
    description: str
    amount: float
    category: str
    # Validated at the boundary: /api/data and /api/spending/data both
    # parse these with fromisoformat, so one unparseable row used to
    # 500 the entire spending page with no way to remove it from the UI.
    start_date: datetime.date
    frequency: Literal["one-time", "monthly", "yearly"]
    end_date: datetime.date | None = None


class TaxDocUpdate(BaseModel):
    amount: float | None = None
    category: str | None = None
    # The UI may only assert the user's own confirmation. Accepting a
    # free string let any value land in the lifecycle column.
    status: Literal["USER_VERIFIED"] | None = None
    issuer: str | None = None


@app.get("/api/session")
async def session(request: Request):
    """Return the identity asserted by the trusted access boundary."""
    return {
        "authentication_mode": os.getenv(
            "HOUSEBOOK_AUTH_MODE", _AUTH_DISABLED,
        ).strip(),
        "email": request.state.authenticated_email,
        "authentication_path": request.state.authentication_path,
    }


def get_db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/api/health")
async def health():
    """Lightweight liveness check."""
    try:
        conn = get_db_conn()
        conn.execute("SELECT 1")
        conn.close()
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "error", "db": str(e)}


@app.get("/api/stats")
async def stats():
    """Aggregate counts for agent/dashboard use."""
    conn = get_db_conn()
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM transactions")
    total_tx = c.fetchone()[0]

    c.execute(
        "SELECT COUNT(*) FROM transactions "
        "WHERE needs_review = 1"
    )
    pending = c.fetchone()[0]

    c.execute(
        "SELECT COUNT(*) FROM transactions "
        "WHERE status = 'AGENT_VERIFIED'"
    )
    agent_verified = c.fetchone()[0]

    c.execute(
        "SELECT COUNT(*) FROM transactions "
        "WHERE status = 'USER_VERIFIED'"
    )
    user_verified = c.fetchone()[0]

    c.execute(
        "SELECT COUNT(*) FROM transactions "
        "WHERE status = 'RECONCILED'"
    )
    reconciled = c.fetchone()[0]

    c.execute(
        "SELECT COUNT(*) FROM transactions "
        "WHERE linked_transaction_id IS NOT NULL"
    )
    linked = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM tax_documents")
    total_tax = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM ingestion_errors")
    errors = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM categorization_rules")
    rules = c.fetchone()[0]

    schema_ver = get_schema_version(DB_PATH)

    conn.close()

    return {
        "transactions": total_tx,
        "pending_review": pending,
        "agent_verified": agent_verified,
        "user_verified": user_verified,
        "reconciled": reconciled,
        "linked_cancelled": linked,
        "tax_documents": total_tax,
        "ingestion_errors": errors,
        "categorization_rules": rules,
        "schema_version": schema_ver,
    }


def _default_date_range():
    today = datetime.date.today()
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT date FROM transactions ORDER BY date DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    latest_txn = row[0] if row else today.isoformat()
    max_date = max(latest_txn, today.isoformat())
    one_year_ago = (
        datetime.date.fromisoformat(max_date)
        - datetime.timedelta(days=365)
    ).isoformat()
    return one_year_ago, max_date


@app.get("/", response_class=HTMLResponse)
async def read_root():
    return RedirectResponse(url="/spending", status_code=302)


@app.get("/spending", response_class=HTMLResponse)
async def spending_page(request: Request):
    one_year_ago, max_date = _default_date_range()
    return templates.TemplateResponse(
        request,
        "spending.html",
        {
            "one_year_ago": one_year_ago,
            "max_date": max_date,
            "active_module": "spending",
        },
    )


@app.get("/spending/trip/{trip_id}", response_class=HTMLResponse)
async def trip_page(request: Request, trip_id: int):
    return templates.TemplateResponse(
        request,
        "trip.html",
        {
            "trip_id": trip_id,
            "active_module": "spending",
            "help_topic": "trips",
        },
    )


@app.get("/api/spending/trip/{trip_id}")
async def get_trip_detail(trip_id: int):
    conn = get_db_conn()
    c = conn.cursor()

    c.execute(
        "SELECT id, name, start_date, end_date, "
        "status, type, location "
        "FROM trips WHERE id = ?",
        (trip_id,),
    )
    trip_row = c.fetchone()
    if not trip_row:
        conn.close()
        return {"error": "Trip not found"}
    trip = dict(trip_row)

    c.execute(f"""
        SELECT t.id, t.date, t.category, t.description,
               t.amount, t.source, t.needs_review
        FROM transactions t
        WHERE t.trip_id = ?
          AND {SPEND_FILTER_T}
        ORDER BY t.date ASC
    """, (trip_id,))
    transactions = [dict(row) for row in c.fetchall()]

    categories = []
    if os.path.exists(RULES_JSON):
        with open(RULES_JSON, "r") as f:
            rules_data = json.load(f)
            categories = sorted(
                [k for k in rules_data.keys()
                     if k not in (CATEGORY_TRANSFERS_REFUNDS,
                                  CATEGORY_CC_PAYMENT)]
            )

    conn.close()

    return {
        "trip": trip,
        "transactions": transactions,
        "categories": categories,
    }


@app.get("/tax", response_class=HTMLResponse)
async def tax_page(request: Request):
    one_year_ago, max_date = _default_date_range()
    return templates.TemplateResponse(
        request,
        "tax.html",
        {
            "one_year_ago": one_year_ago,
            "max_date": max_date,
            "active_module": "tax",
        },
    )


def _dashboard_response(
    *,
    start_date: datetime.date | None,
    end_date: datetime.date | None,
    trip_id: int | None,
    include_tax_docs: bool = False,
) -> dict:
    try:
        return load_spending_dashboard(
            DB_PATH,
            RULES_JSON,
            start_date=start_date,
            end_date=end_date,
            trip_id=trip_id,
            include_tax_docs=include_tax_docs,
        )
    except DashboardQueryError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/data")
async def get_data(
    start_date: datetime.date | None = Query(None),
    end_date: datetime.date | None = Query(None),
    trip_id: int | None = Query(None),
):
    return _dashboard_response(
        start_date=start_date,
        end_date=end_date,
        trip_id=trip_id,
        include_tax_docs=True,
    )


@app.get("/api/spending/data")
async def get_spending_data(
    start_date: datetime.date | None = Query(None),
    end_date: datetime.date | None = Query(None),
    trip_id: int | None = Query(None),
):
    return _dashboard_response(
        start_date=start_date,
        end_date=end_date,
        trip_id=trip_id,
    )


@app.get("/api/tax/data")
async def get_tax_data():
    return {"tax_docs": load_tax_documents(DB_PATH)}


@app.get("/api/transactions/{tx_id}/detail")
async def get_transaction_detail(tx_id: int):
    conn = get_db_conn()
    tx = conn.execute(
        "SELECT id, date, description, amount, category, "
        "source, status, original_file, profile, "
        "needs_review, trip_id, metadata, "
        "source_file_path, source_file_sha256, sidecar_path "
        "FROM transactions WHERE id = ?",
        (tx_id,),
    ).fetchone()
    conn.close()
    if not tx:
        raise HTTPException(status_code=404, detail="transaction not found")
    result = dict(tx)
    if result.get("metadata"):
        try:
            result["metadata"] = json.loads(result["metadata"])
        except (json.JSONDecodeError, TypeError):
            pass
    return {"transaction": result}


@app.post("/api/transactions/{tx_id}/trip")
async def update_trip(tx_id: int, data: TripUpdate):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("UPDATE transactions SET trip_id = ? WHERE id = ?", (data.trip_id, tx_id))
    updated = c.rowcount
    conn.commit()
    conn.close()
    if not updated:
        raise HTTPException(status_code=404, detail="transaction not found")
    return {"status": "success"}


@app.post("/api/transactions/{tx_id}/review")
async def toggle_review(tx_id: int, data: ReviewToggle):
    conn = get_db_conn()
    c = conn.cursor()
    # Clearing needs_review IS the user confirming the row, so it must
    # promote status too. Setting the flag alone let a transaction
    # leave every pending queue while still UNVERIFIED — silently
    # skipping the mandatory review step.
    if data.needs_review:
        c.execute(
            "UPDATE transactions SET needs_review = 1 WHERE id = ?",
            (tx_id,),
        )
    else:
        c.execute(
            "UPDATE transactions SET needs_review = 0, "
            "status = 'USER_VERIFIED' WHERE id = ?",
            (tx_id,),
        )
    updated = c.rowcount
    conn.commit()
    conn.close()
    if not updated:
        raise HTTPException(status_code=404, detail="transaction not found")
    return {"status": "success"}


@app.post("/api/transactions/{tx_id}/category")
async def update_category(tx_id: int, data: CategoryUpdate):
    conn = get_db_conn()
    try:
        c = conn.cursor()

        row = c.execute(
            "SELECT description, category FROM transactions WHERE id = ?",
            (tx_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404, detail="transaction not found",
            )

        # 1. Update the specific transaction
        c.execute(
            "UPDATE transactions SET category = ?, "
            "needs_review = 0, status = 'USER_VERIFIED' "
            "WHERE id = ?",
            (data.category, tx_id),
        )

        # 2. Record a "Learned Rule" from the cleaned description.
        # The table's UNIQUE is (category, keyword), so INSERT OR
        # REPLACE cannot retarget an existing keyword — recategorizing
        # would leave both the old and new rule and let an arbitrary
        # one win. Drop the keyword's prior rows first.
        cleaned_kw = clean_keyword(row["description"] or "").lower()
        if len(cleaned_kw) >= 3:
            c.execute(
                "DELETE FROM categorization_rules WHERE keyword = ?",
                (cleaned_kw,),
            )
            c.execute(
                "INSERT INTO categorization_rules "
                "(category, keyword) VALUES (?, ?)",
                (data.category, cleaned_kw),
            )

        conn.commit()
    finally:
        conn.close()
    return {"status": "success"}


@app.post("/api/trips")
async def create_trip(data: TripCreate):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO trips "
        "(name, start_date, end_date, status, type, location) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (data.name, data.start_date, data.end_date,
         data.status, data.type, data.location),
    )
    conn.commit()
    conn.close()
    return {"status": "success"}


@app.get("/api/tax_docs/{doc_id}")
async def get_tax_doc_detail(doc_id: int):
    conn = get_db_conn()
    row = conn.execute(
        "SELECT raw_data, source_file_path, sidecar_path "
        "FROM tax_documents WHERE id = ?", (doc_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="tax document not found")
    return {
        "raw_data": row["raw_data"] or "{}",
        "source_file_path": row["source_file_path"],
        "sidecar_path": row["sidecar_path"],
    }


@app.patch("/api/tax_docs/{doc_id}")
async def update_tax_doc(doc_id: int, data: TaxDocUpdate):
    conn = get_db_conn()
    c = conn.cursor()
    fields = []
    values = []
    if data.amount is not None:
        fields.append("amount = ?")
        values.append(data.amount)
    if data.category is not None:
        fields.append("category = ?")
        values.append(data.category)
    if data.status is not None:
        fields.append("status = ?")
        values.append(data.status)
    if data.issuer is not None:
        fields.append("issuer = ?")
        values.append(data.issuer)
    if not fields:
        conn.close()
        return {"status": "no_changes"}

    # Any accepted edit came from the user-facing UI. Keep the lifecycle
    # and review flag atomic so a corrected document cannot remain in the
    # pending queue or carry a stale UNVERIFIED status.
    if data.status is None:
        fields.append("status = 'USER_VERIFIED'")
    fields.append("needs_review = 0")

    values.append(doc_id)
    c.execute(
        f"UPDATE tax_documents SET {', '.join(fields)} "
        f"WHERE id = ?",
        values,
    )
    updated = c.rowcount
    conn.commit()
    conn.close()
    if not updated:
        raise HTTPException(status_code=404, detail="tax document not found")
    return {"status": "success"}


@app.post("/api/tax_docs/{doc_id}/review")
async def toggle_tax_review(doc_id: int, data: ReviewToggle):
    conn = get_db_conn()
    c = conn.cursor()
    if data.needs_review:
        c.execute(
            "UPDATE tax_documents SET needs_review = 1 WHERE id = ?",
            (doc_id,),
        )
    else:
        c.execute(
            "UPDATE tax_documents SET needs_review = 0, "
            "status = 'USER_VERIFIED' WHERE id = ?",
            (doc_id,),
        )
    updated = c.rowcount
    conn.commit()
    conn.close()
    if not updated:
        raise HTTPException(status_code=404, detail="tax document not found")
    return {"status": "success"}


@app.delete("/api/tax_docs/{doc_id}")
async def delete_tax_doc(doc_id: int):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("DELETE FROM tax_documents WHERE id = ?", (doc_id,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="tax document not found")
    return {"status": "success"}


@app.get("/api/tax_estimate")
async def get_tax_estimate(year: int = Query(default=2025)):
    from housebook.tax.estimate import compute_tax_estimate
    return compute_tax_estimate(DB_PATH, year)


@app.get("/api/manual_expenses")
async def get_manual_expenses():
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, description, amount, category, "
        "start_date, end_date, frequency "
        "FROM manual_expenses"
    )
    expenses = [dict(row) for row in c.fetchall()]
    conn.close()
    return expenses


@app.post("/api/manual_expenses")
async def create_manual_expense(data: ManualExpenseCreate):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO manual_expenses "
        "(description, amount, category, start_date, end_date, frequency) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            data.description,
            data.amount,
            data.category,
            data.start_date.isoformat(),
            data.end_date.isoformat() if data.end_date else None,
            data.frequency,
        ),
    )
    conn.commit()
    conn.close()
    return {"status": "success"}


@app.put("/api/manual_expenses/{exp_id}")
async def update_manual_expense(exp_id: int, data: ManualExpenseCreate):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE manual_expenses SET description = ?, amount = ?, "
        "category = ?, start_date = ?, end_date = ?, frequency = ? "
        "WHERE id = ?",
        (
            data.description,
            data.amount,
            data.category,
            data.start_date.isoformat(),
            data.end_date.isoformat() if data.end_date else None,
            data.frequency,
            exp_id,
        ),
    )
    updated = c.rowcount
    conn.commit()
    conn.close()
    if not updated:
        raise HTTPException(status_code=404, detail="manual expense not found")
    return {"status": "success"}


@app.delete("/api/manual_expenses/{exp_id}")
async def delete_manual_expense(exp_id: int):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "DELETE FROM manual_expenses WHERE id = ?",
        (exp_id,),
    )
    deleted = c.rowcount
    conn.commit()
    conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="manual expense not found")
    return {"status": "success"}


# ── HSA Shoebox ────────────────────────────────────────────────


@app.get("/hsa", response_class=HTMLResponse)
async def hsa_page(request: Request):
    one_year_ago, max_date = _default_date_range()
    return templates.TemplateResponse(
        request,
        "hsa.html",
        {
            "one_year_ago": one_year_ago,
            "max_date": max_date,
            "active_module": "hsa",
        },
    )


@app.get("/api/hsa/data")
async def get_hsa_data(
    year: int = Query(None),
    status: str = Query(None),
    patient: str = Query(None),
):
    conn = get_db_conn()

    where = "WHERE e.status != 'DELETED'"
    params = []
    if year:
        where += " AND strftime('%Y', e.service_date) = ?"
        params.append(str(year))
    if status:
        where += " AND e.status = ?"
        params.append(status.upper())
    if patient:
        where += " AND e.patient = ?"
        params.append(patient)

    expenses = conn.execute(
        f"""SELECT e.id, e.service_date, e.provider, e.patient,
                   e.description, e.amount_billed,
                   e.insurance_paid, e.patient_responsibility,
                   e.category, e.payment_method, e.source,
                   e.status, e.needs_review, e.transaction_id,
                   e.evidence_level, e.notes, e.exclusion_reason,
                   COUNT(d.id) AS doc_count
            FROM hsa_expenses e
            LEFT JOIN hsa_documents d ON d.expense_id = e.id
            {where}
            GROUP BY e.id
            ORDER BY e.service_date DESC, e.id DESC""",
        params,
    ).fetchall()

    summary = conn.execute(
        f"""SELECT
                status,
                COUNT(*) AS count,
                COALESCE(SUM(patient_responsibility), 0) AS total
            FROM hsa_expenses {where.replace('e.', '')}
            GROUP BY status""",
        params,
    ).fetchall()

    reimbursable = conn.execute(
        f"""SELECT COALESCE(SUM(patient_responsibility), 0)
            AS total FROM hsa_expenses
            {where.replace('e.', '')}
            AND status = 'UNREIMBURSED'
            AND exclusion_reason IS NULL
            AND evidence_level IN ('ready', 'strong')""",
        params,
    ).fetchone()["total"]

    excluded_data = conn.execute(
        f"""SELECT COUNT(*) AS count,
            COALESCE(SUM(patient_responsibility), 0) AS total
            FROM hsa_expenses {where.replace('e.', '')}
            AND exclusion_reason IS NOT NULL""",
        params,
    ).fetchone()

    years = conn.execute(
        "SELECT DISTINCT strftime('%Y', service_date) AS yr "
        "FROM hsa_expenses WHERE service_date IS NOT NULL "
        "ORDER BY yr DESC"
    ).fetchall()

    patients = conn.execute(
        "SELECT DISTINCT patient FROM hsa_expenses "
        "WHERE patient IS NOT NULL ORDER BY patient"
    ).fetchall()

    conn.close()
    return {
        "expenses": [dict(r) for r in expenses],
        "summary": [dict(r) for r in summary],
        "reimbursable_total": reimbursable,
        "excluded_count": excluded_data["count"],
        "excluded_total": excluded_data["total"],
        "years": [r["yr"] for r in years],
        "patients": [r["patient"] for r in patients],
    }


@app.patch("/api/hsa/{expense_id}")
async def update_hsa_expense(expense_id: int, request: Request):
    data = await request.json()
    conn = get_db_conn()
    c = conn.cursor()

    row = c.execute(
        "SELECT * FROM hsa_expenses WHERE id = ?",
        (expense_id,),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="HSA expense not found")

    allowed = {
        "provider", "patient", "category", "description",
        "patient_responsibility", "amount_billed",
        "insurance_paid", "payment_method", "status",
        "evidence_level", "notes", "exclusion_reason",
    }
    updates = []
    params = []
    for field, value in data.items():
        if field in allowed:
            c.execute(
                "INSERT INTO hsa_audit_log "
                "(table_name, record_id, field_name, "
                "old_value, new_value, changed_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("hsa_expenses", expense_id, field,
                 str(row[field]) if row[field] is not None
                 else None,
                 str(value), request.state.authenticated_email),
            )
            updates.append(f"{field} = ?")
            params.append(value)

    if updates:
        updates.append("updated_at = CURRENT_TIMESTAMP")
        sql = (
            f"UPDATE hsa_expenses "
            f"SET {', '.join(updates)} WHERE id = ?"
        )
        params.append(expense_id)
        c.execute(sql, params)
        conn.commit()

    conn.close()
    return {"status": "success"}


@app.post("/api/hsa/{expense_id}/review")
async def toggle_hsa_review(
    expense_id: int, data: ReviewToggle,
):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE hsa_expenses SET needs_review = ?, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (1 if data.needs_review else 0, expense_id),
    )
    updated = c.rowcount
    conn.commit()
    conn.close()
    if not updated:
        raise HTTPException(status_code=404, detail="HSA expense not found")
    return {"status": "success"}


@app.delete("/api/hsa/{expense_id}")
async def delete_hsa_expense(expense_id: int, request: Request):
    conn = get_db_conn()
    c = conn.cursor()
    row = c.execute(
        "SELECT status FROM hsa_expenses WHERE id = ?",
        (expense_id,),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="HSA expense not found")
    c.execute(
        "INSERT INTO hsa_audit_log "
        "(table_name, record_id, field_name, "
        "old_value, new_value, changed_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("hsa_expenses", expense_id, "status",
         row["status"], "DELETED",
         request.state.authenticated_email),
    )
    c.execute(
        "UPDATE hsa_expenses SET status = 'DELETED', "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (expense_id,),
    )
    conn.commit()
    conn.close()
    return {"status": "success"}


@app.get("/api/hsa/{expense_id}/detail")
async def get_hsa_detail(expense_id: int):
    conn = get_db_conn()

    expense = conn.execute(
        "SELECT * FROM hsa_expenses WHERE id = ?",
        (expense_id,),
    ).fetchone()
    if not expense:
        conn.close()
        raise HTTPException(status_code=404, detail="HSA expense not found")

    documents = conn.execute(
        "SELECT id, document_type, file_path, file_hash, "
        "original_filename, raw_data, ingested_at "
        "FROM hsa_documents WHERE expense_id = ?",
        (expense_id,),
    ).fetchall()

    audit_log = conn.execute(
        "SELECT field_name, old_value, new_value, "
        "changed_by, changed_at, reason "
        "FROM hsa_audit_log "
        "WHERE table_name = 'hsa_expenses' "
        "AND record_id = ? ORDER BY changed_at DESC",
        (expense_id,),
    ).fetchall()

    linked_tx = None
    if expense["transaction_id"]:
        linked_tx = conn.execute(
            "SELECT id, date, description, amount, "
            "category, source, original_file "
            "FROM transactions WHERE id = ?",
            (expense["transaction_id"],),
        ).fetchone()

    conn.close()

    docs_out = []
    for d in documents:
        raw = None
        if d["raw_data"]:
            try:
                raw = json.loads(d["raw_data"])
            except (json.JSONDecodeError, TypeError):
                raw = d["raw_data"]
        docs_out.append({
            "id": d["id"],
            "document_type": d["document_type"],
            "file_path": d["file_path"],
            "original_filename": d["original_filename"],
            "file_hash": d["file_hash"],
            "ingested_at": d["ingested_at"],
            "raw_data": raw,
        })

    return {
        "expense": dict(expense),
        "documents": docs_out,
        "audit_log": [dict(r) for r in audit_log],
        "linked_transaction": dict(linked_tx)
        if linked_tx else None,
    }


def _resolve_in_workspace(path: str) -> str | None:
    """Resolve a path and confine it to the workspace.

    Returns the absolute path, or None if it escapes the workspace.
    Rejecting `..` textually is not enough: an absolute path, a
    symlink, or an encoded traversal all bypass that check, so the
    containment test is done on the *resolved* path.
    """
    root = os.path.realpath(str(WORKSPACE_DIR))
    candidate = os.path.realpath(os.path.join(root, path))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate


def _serve_pdf(file_path: str):
    from fastapi.responses import FileResponse

    resolved = _resolve_in_workspace(file_path)
    if resolved is None:
        raise HTTPException(status_code=403, detail="path outside workspace")
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="file not found on disk")

    return FileResponse(
        resolved,
        media_type="application/pdf",
        headers={
            "Content-Disposition": "inline",
        },
    )


def _serve_registered_workspace_pdf(file_path: str):
    """Serve only statement PDFs already registered in the ledger.

    The former path-only endpoint accepted every file under the workspace,
    including JSON configuration and locally stored API credentials. The UI
    only needs source statements referenced by transaction or tax rows, so
    fail closed to that set and to PDF files.
    """
    resolved = _resolve_in_workspace(file_path)
    if resolved is None:
        raise HTTPException(status_code=403, detail="path outside workspace")
    if not resolved.lower().endswith(".pdf"):
        raise HTTPException(status_code=403, detail="only PDF statements allowed")

    conn = get_db_conn()
    try:
        registered = conn.execute(
            "SELECT 1 FROM transactions "
            "WHERE source_file_path = ? OR original_file = ? "
            "UNION ALL "
            "SELECT 1 FROM tax_documents "
            "WHERE source_file_path = ? OR original_file = ? "
            "LIMIT 1",
            (file_path, file_path, file_path, file_path),
        ).fetchone()
    finally:
        conn.close()

    if not registered:
        raise HTTPException(status_code=403, detail="statement is not registered")
    return _serve_pdf(file_path)


@app.get("/api/hsa/doc/{doc_id}/file")
async def serve_hsa_document(doc_id: int):
    conn = get_db_conn()
    doc = conn.execute(
        "SELECT file_path FROM hsa_documents WHERE id = ?",
        (doc_id,),
    ).fetchone()
    conn.close()

    if not doc:
        raise HTTPException(status_code=404, detail="not found")

    return _serve_pdf(doc["file_path"])


@app.get("/api/workspace/file")
async def serve_workspace_file(path: str = Query(...)):
    """Serve a registered workspace-relative statement PDF."""
    return _serve_registered_workspace_pdf(path)


def main():
    import uvicorn

    # Keep Uvicorn behind the authenticated TLS proxy. The shared proxy
    # credential is a second boundary, not permission to expose the
    # application server directly to the LAN.
    host = os.getenv("HOUSEBOOK_HOST", "127.0.0.1")
    if not _is_loopback_host(host):
        raise RuntimeError(
            "HOUSEBOOK_HOST must be a loopback address; "
            "publish the dashboard through its authenticated proxy",
        )
    uvicorn.run("housebook.app:app", host=host, port=8000)


if __name__ == "__main__":
    main()
