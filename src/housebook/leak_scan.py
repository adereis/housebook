"""Find real workspace data in the repository's tracked files and commit
messages.

A denylist only catches strings someone already knows are private. The
leaks that actually happen are copies: a real row becomes a fixture or
an SOP example, its people and companies are renamed, and its numbers
stay. This scanner reads the live workspace read-only and reports any
tracked text that reproduces it:

- identifiers (Amazon order IDs, bank reference codes, claim, ticket,
  agreement, Rx and NDC numbers) anywhere;
- card last4 digits next to card context (``last4``, ``__1234__``, ``*``);
- a date and an amount from the same real record within a few lines;
- distinctive standalone amounts from medical, tax, statement-balance
  and off-ledger records;
- names: the PII denylist, home location, patients, cardholders,
  passengers, providers, medications and tax issuers;
- trips: a trip's exact start and end dates together, or one of its
  places next to a date in the trip's months.

The threshold is one attribute of a real record versus two. A place
alone ("Paris") or a date alone is common and identifies nothing, so
fixtures may echo one. Two from the same record, such as a trip's
place with its month, pin down when the household was away.

It scans the git index (what the next commit contains) by default, the
tree of any commit with ``--rev``, a commit message file with
``--message`` (the commit-msg hook), or every commit in a ``git
rev-list`` range, message and tree, with ``--commits`` (the pre-push
hook). Findings that are intentional, such as the author's name in
LICENSE, go in ``$WORKSPACE/config/leak-scan-allow.txt`` as ``<path
glob> <text>``; a commit message's path is ``COMMIT_MSG``.
"""

from __future__ import annotations

import argparse
import bisect
import calendar
import datetime
import fnmatch
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

# Generated or vendored: not authored here, and large enough to yield
# only coincidental matches.
SKIPPED_PREFIXES = (
    "package-lock.json",
    "src/housebook/static/css/",
    "src/housebook/static/vendor/",
)

# The path a commit message is reported under, and matched against in
# the allowlist.
MESSAGE_PATH = "COMMIT_MSG"

# Sidecar/metadata keys whose values identify one real document.
IDENTIFIER_KEYS = {
    "account_number", "agreement_number", "amazon_order_id", "claim_id",
    "member_id", "ndc", "policy_number", "rx_number", "ticket_number",
    "trip_code",
}
# Keys holding a medication ("<Drug> 2mg/mL pen"). Its first word is the
# drug's name; the rest is dose and form, too generic to flag.
DRUG_KEYS = {"drug"}
# Keys whose values are people's names. Statement parsers write these as
# "SURNAME/GIVEN ..." with fare text glued on ("DOE/PAID ECONOMY"), so
# only the surname block is kept. Cardholder names are left to the
# denylist: `account.name` sometimes holds a card product instead.
NAME_KEYS = {"passenger_name", "recipient", "renter_name"}
# Field labels that parsers sometimes copy into those values.
NAME_STOPWORDS = {"name", "passenger", "recipient", "renter"}
# How far apart (in lines) a date and its amount may sit in a fixture.
PAIR_WINDOW = 4
PLACEHOLDER_VALUES = {"", "redacted", "your name", "cardholder name"}

_DATE = re.compile(r"\b(?:19|20)\d\d-[01]\d-[0-3]\d\b")
_AMOUNT = re.compile(
    r"(?<![\w.])\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{1,2})?(?![\w]|[.-]\d)"
)
_TOKEN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
_FOUR_DIGITS = re.compile(r"(?<!\d)\d{4}(?!\d)")
_BANK_REFERENCE = re.compile(r"\*([A-Z0-9]{8,})\b")
_CARD_CONTEXT = ("last4", "last 4", "ending", "card", "acct", "account",
                 "*", "__", "xx")

# Ways a text can name a month: 2026-04(-09), 04/09/2026 or 09/04/2026
# (either order, since statements abroad print day first), and
# "Apr 2026" / "April 9, 2026" / "Apr 9-28, 2026".
_ISO_MONTH = re.compile(r"\b((?:19|20)\d\d)-([01]\d)\b")
_NUMERIC_DATE = re.compile(r"\b([0-3]?\d)[/-]([0-3]?\d)[/-]((?:19|20)\d\d)\b")
_MONTH_NUMBERS = {
    name.lower(): number
    for names in (calendar.month_name, calendar.month_abbr)
    for number, name in enumerate(names) if name
} | {"sept": 9}
_NAMED_MONTH = re.compile(
    r"\b(" + "|".join(sorted(_MONTH_NUMBERS, key=len, reverse=True)) + r")"
    r"\.?\s+(?:[0-3]?\d(?:\s*[-–]\s*[0-3]?\d)?(?:st|nd|rd|th)?,?\s+)?"
    r"((?:19|20)\d\d)\b",
    re.IGNORECASE,
)
# A trip location such as "Rome & Florence, Italy" or "Raleigh, NC"
# splits into places; state and country codes are too short to matter.
_PLACE_SEPARATORS = re.compile(r"[,&/]")
MIN_PLACE_LENGTH = 4


@dataclass(frozen=True)
class TripNeedle:
    """A real trip: when the household was away, and where."""

    trip_id: int
    start: str | None
    end: str | None
    # (lowercase place, pattern) per place in the trip's location.
    places: tuple[tuple[str, re.Pattern], ...]
    months: frozenset[tuple[int, int]]


@dataclass
class Needles:
    """Everything real that must not appear in tracked text."""

    # (kind, pattern, literal): a lowercase literal lets the scan skip
    # the regex for files that cannot match; denylist regexes have none.
    patterns: list[tuple[str, re.Pattern, str | None]] = field(
        default_factory=list)
    identifiers: dict[str, str] = field(default_factory=dict)
    last4: set[str] = field(default_factory=set)
    amounts: dict[Decimal, str] = field(default_factory=dict)
    dated: dict[tuple[str, Decimal], str] = field(default_factory=dict)
    trips: list[TripNeedle] = field(default_factory=list)
    # What the scan cannot check, reported so its coverage is visible.
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    text: str
    source: str


# ── needles ─────────────────────────────────────────────────────────


def _money(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = abs(Decimal(str(value).replace(",", "")))
    except InvalidOperation:
        return None
    return amount.quantize(Decimal("0.01"))


def _is_distinctive(amount: Decimal) -> bool:
    """True for figures a person would not pick at random.

    Cents make a value specific; so does a whole number of 1000+ that is
    neither a round hundred nor year-like.
    """
    if amount != amount.to_integral_value():
        return amount >= 10
    return amount >= 1000 and amount % 100 != 0 and not 1900 <= amount <= 2100


def _is_round(amount: Decimal) -> bool:
    """Whole multiples of five are what people invent for fixtures."""
    return amount == amount.to_integral_value() and amount % 5 == 0


class _Collector:
    def __init__(self):
        self.needles = Needles()
        self._names: dict[str, str] = {}

    def name(self, value, source: str, whole: bool = False):
        """Record a person or organization name.

        Free-form names ("DOE/JANE MS") are split into words; ``whole``
        keeps a multi-word organization name as one phrase.
        """
        if not isinstance(value, str):
            return
        text = value.strip()
        if text.lower() in PLACEHOLDER_VALUES:
            return
        parts = [text] if whole else re.findall(r"[A-Za-z]{4,}", text)
        for part in parts:
            if len(part) >= 4 and part.lower() not in NAME_STOPWORDS:
                self._names.setdefault(part.lower(), source)

    def identifier(self, value, source: str):
        text = str(value).strip() if value is not None else ""
        if len(text) >= 6 and any(c.isdigit() for c in text):
            self.needles.identifiers.setdefault(text, source)

    def amount(self, value, source: str):
        amount = _money(value)
        if amount is not None and _is_distinctive(amount):
            self.needles.amounts.setdefault(amount, source)

    def dated(self, date, value, source: str):
        amount = _money(value)
        if (isinstance(date, str) and _DATE.fullmatch(date[:10])
                and amount is not None and amount >= 1
                and not _is_round(amount)):
            self.needles.dated.setdefault((date[:10], amount), source)

    def walk(self, node, source: str, amounts: bool):
        """Collect from a sidecar/metadata tree.

        ``amounts`` also records every distinctive number as a standalone
        needle; statement sidecars only do so for their balances.
        """
        if isinstance(node, dict):
            if "date" in node and "amount" in node:
                self.dated(node["date"], node["amount"], source)
            for key, value in node.items():
                if key in IDENTIFIER_KEYS:
                    self.identifier(value, source)
                elif key == "last4" and re.fullmatch(r"\d{4}", str(value)):
                    self.needles.last4.add(str(value))
                elif key in NAME_KEYS and isinstance(value, str):
                    self.name(value.split("/")[0], source)
                elif key in DRUG_KEYS and isinstance(value, str):
                    self.name(value.split()[0] if value.split() else "",
                              "medications")
                elif key in ("balances", "payment"):
                    self.walk(value, source, amounts=True)
                    continue
                self.walk(value, source, amounts)
        elif isinstance(node, list):
            for value in node:
                self.walk(value, source, amounts)
        elif amounts and isinstance(node, (int, float)):
            self.amount(node, source)

    def trip(self, trip_id, location, start, end) -> tuple[bool, bool]:
        """Record a trip; return whether it has places and full dates."""
        places = []
        for part in _PLACE_SEPARATORS.split(location or ""):
            place = " ".join(part.split())
            if len(place) >= MIN_PLACE_LENGTH:
                places.append((place.lower(), re.compile(
                    rf"\b{re.escape(place)}\b", re.IGNORECASE)))
        where = f"trip {trip_id}"
        first, last = _iso_date(start, where), _iso_date(end, where)
        months = set()
        if first and last:
            year, month = first.year, first.month
            while (year, month) <= (last.year, last.month):
                months.add((year, month))
                year, month = year + month // 12, month % 12 + 1
        self.needles.trips.append(TripNeedle(
            trip_id, start if first else None, end if last else None,
            tuple(places), frozenset(months)))
        return bool(places), bool(months)

    def finish(self) -> Needles:
        for name, source in self._names.items():
            self.needles.patterns.append((
                f"name ({source})",
                re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE),
                name,
            ))
        return self.needles


def _read_json(path: Path):
    """Parse an optional workspace JSON file; a broken one is fatal.

    Skipping it would silently shrink what the scan can detect.
    """
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(f"housebook-leak-scan: cannot read {path}: {e}")


def _json_column(value, where: str):
    """Parse a JSON column value, failing loudly like `_read_json`."""
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError as e:
        raise SystemExit(f"housebook-leak-scan: {where} is not JSON: {e}")


def _iso_date(value, where: str) -> datetime.date | None:
    """Parse an optional YYYY-MM-DD column, failing loudly when malformed."""
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError as e:
        raise SystemExit(f"housebook-leak-scan: {where} has a bad date: {e}")


def load_needles(workspace: Path, db_path: Path) -> Needles:
    """Build the needle set from a workspace, opening its DB read-only."""
    c = _Collector()
    config = workspace / "config"

    denylist = config / "pii-denylist.txt"
    if denylist.exists():
        for raw in denylist.read_text().splitlines():
            pattern = raw.strip()
            if pattern and not pattern.startswith("#"):
                c.needles.patterns.append(
                    ("denylist", re.compile(pattern, re.IGNORECASE), None),
                )

    profile = _read_json(config / "user_profile.json") or {}
    c.name(profile.get("full_name"), "user profile", whole=True)
    home = profile.get("home_location") or {}
    for key in ("city", "zip"):
        value = str(home.get(key) or "").strip()
        if value:
            c.needles.patterns.append((
                "home location",
                re.compile(rf"\b{re.escape(value)}\b", re.IGNORECASE),
                value.lower(),
            ))

    patients = _read_json(config / "hsa" / "patients.json") or {}
    for patient in patients.get("patients", []):
        c.name(patient.get("name"), "HSA patients")
    providers = _read_json(config / "hsa" / "providers.json") or {}
    for provider in providers.get("providers", []):
        for value in [provider.get("canonical_name"),
                      *provider.get("aliases", [])]:
            c.name(value, "HSA providers", whole=True)

    for source in ("cc", "hsa", "tax"):
        for sidecar in sorted((workspace / source).glob("**/*.json")):
            stem = sidecar.name
            if source == "cc":  # tax names carry form numbers (__1099__)
                for digits in re.findall(r"__(\d{4})__", stem):
                    c.needles.last4.add(digits)
            tree = _read_json(sidecar)
            if tree is not None:
                c.walk(tree, f"{source} sidecar {stem}",
                       amounts=source != "cc")

    if db_path.exists():
        _load_database(c, db_path)
    return c.finish()


def _load_database(c: _Collector, db_path: Path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "transactions" in tables:
            for date, amount, description, metadata in conn.execute(
                "SELECT date, amount, description, metadata "
                "FROM transactions"
            ):
                source = f"transaction {date}"
                c.dated(date, amount, source)
                for ref in _BANK_REFERENCE.findall(description or ""):
                    if re.search(r"\d", ref) and re.search(r"[A-Z]", ref):
                        c.identifier(ref, source)
                tree = _json_column(metadata, f"{source} metadata")
                if tree is not None:
                    c.walk(tree, source, amounts=False)
        if "hsa_expenses" in tables:
            for row in conn.execute(
                "SELECT service_date, payment_date, provider, amount_billed, "
                "insurance_paid, patient_responsibility FROM hsa_expenses"
            ):
                service, paid_on, provider, *amounts = row
                source = f"HSA expense {service} {provider}"
                for amount in amounts:
                    c.amount(amount, source)
                    c.dated(service, amount, source)
                    c.dated(paid_on, amount, source)
        if "hsa_documents" in tables:
            for doc_id, raw in conn.execute(
                "SELECT id, raw_data FROM hsa_documents"
            ):
                tree = _json_column(raw, f"hsa_documents {doc_id} raw_data")
                if tree is not None:
                    c.walk(tree, "HSA document", amounts=True)
        if "manual_expenses" in tables:
            for description, amount, start in conn.execute(
                "SELECT description, amount, start_date FROM manual_expenses"
            ):
                source = f"manual expense {description}"
                c.amount(amount, source)
                c.dated(start, amount, source)
        if "tax_documents" in tables:
            for year, issuer, amount, raw in conn.execute(
                "SELECT tax_year, issuer, amount, raw_data FROM tax_documents"
            ):
                source = f"tax document {year} {issuer}"
                c.name(issuer, "tax issuers", whole=True)
                c.amount(amount, source)
                tree = _json_column(raw, f"{source} raw_data")
                if tree is not None:
                    c.walk(tree, source, amounts=True)
        if "trips" in tables:
            unplaced, undated = [], []
            for trip_id, location, start, end in conn.execute(
                "SELECT id, location, start_date, end_date FROM trips "
                "ORDER BY id"
            ):
                has_places, has_dates = c.trip(trip_id, location, start, end)
                if not has_places:
                    unplaced.append(str(trip_id))
                if not has_dates:
                    undated.append(str(trip_id))
            if unplaced:
                c.needles.notes.append(
                    f"trips {', '.join(unplaced)} have no location, so no "
                    f"place of theirs is checked. Set one with "
                    f"`housebook-audit edit-trip <id> --location ...`.")
            if undated:
                c.needles.notes.append(
                    f"trips {', '.join(undated)} lack a start or end "
                    f"date, so their dates are not checked.")
    finally:
        conn.close()


# ── tracked text ────────────────────────────────────────────────────


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin, capture_output=True, check=True,
    ).stdout


def _tree_blobs(repo: Path, rev: str | None = None) -> dict[str, str]:
    """Return {path: blob sha} for the index (default) or a commit's tree.

    Submodules and generated/vendored files are left out.
    """
    blobs: dict[str, str] = {}
    if rev is None:
        for record in _git(repo, "ls-files", "-s", "-z").split(b"\0"):
            if record:
                meta, path = record.split(b"\t", 1)
                mode, sha, _stage = meta.split()
                if mode != b"160000":
                    blobs[path.decode()] = sha.decode()
    else:
        for record in _git(repo, "ls-tree", "-r", "-z", rev).split(b"\0"):
            if record:
                meta, path = record.split(b"\t", 1)
                _mode, kind, sha = meta.split()
                if kind == b"blob":
                    blobs[path.decode()] = sha.decode()
    return {
        path: sha for path, sha in blobs.items()
        if not path.startswith(SKIPPED_PREFIXES)
    }


def _cat_objects(repo: Path, shas) -> dict[str, bytes]:
    """Return {sha: raw content} for git objects, in one batch call."""
    shas = list(dict.fromkeys(shas))
    if not shas:
        return {}
    batch = _git(repo, "cat-file", "--batch",
                 stdin=("\n".join(shas) + "\n").encode())
    contents: dict[str, bytes] = {}
    pos = 0
    for sha in shas:
        header_end = batch.index(b"\n", pos)
        size = int(batch[pos:header_end].split()[2])
        contents[sha] = batch[header_end + 1:header_end + 1 + size]
        pos = header_end + 1 + size + 1
    return contents


def _blob_texts(repo: Path, shas) -> dict[str, str]:
    """Return {sha: text} for blobs, leaving binary ones out."""
    return {
        sha: data.decode("utf-8", errors="replace")
        for sha, data in _cat_objects(repo, shas).items()
        if b"\0" not in data
    }


def tracked_texts(repo: Path, rev: str | None = None) -> dict[str, str]:
    """Return {path: text} for the index (default) or a commit's tree.

    Binary blobs, submodules and generated/vendored files are skipped.
    """
    blobs = _tree_blobs(repo, rev)
    texts = _blob_texts(repo, blobs.values())
    return {path: texts[sha] for path, sha in sorted(blobs.items())
            if sha in texts}


# ── matching ────────────────────────────────────────────────────────


def _line_amounts(line: str) -> list[tuple[Decimal, str]]:
    """Amount-like numbers on a line, ignoring the digits inside dates."""
    blanked = _DATE.sub(lambda m: " " * len(m.group()), line)
    found = []
    for match in _AMOUNT.finditer(blanked):
        amount = _money(match.group(1) + (match.group(2) or ""))
        if amount is not None:
            found.append((amount, match.group().strip()))
    return found


def _line_months(line: str) -> set[tuple[int, int]]:
    """The (year, month) pairs a line names, in any common date format."""
    months = {(int(y), int(m)) for y, m in _ISO_MONTH.findall(line)}
    for first, second, year in _NUMERIC_DATE.findall(line):
        months |= {(int(year), int(n)) for n in (first, second)}
    for name, year in _NAMED_MONTH.findall(line):
        months.add((int(year), _MONTH_NUMBERS[name.lower()]))
    return {(y, m) for y, m in months if 1 <= m <= 12}


def _trip_findings(path: str, text: str, lines: list[str],
                   line_starts: list[int], lowered: str,
                   trips: list[TripNeedle]) -> list[Finding]:
    """A trip's exact span, or one of its places beside one of its months.

    Either pins down when the household was away. A place or a date on
    its own does not, and fixtures may use one.
    """
    findings: list[Finding] = []
    months_by_line: dict[int, set[tuple[int, int]]] = {}

    def months_near(i: int) -> set[tuple[int, int]]:
        near = set()
        for j in range(max(0, i - PAIR_WINDOW),
                       min(len(lines), i + PAIR_WINDOW + 1)):
            if j not in months_by_line:
                months_by_line[j] = _line_months(lines[j])
            near |= months_by_line[j]
        return near

    for trip in trips:
        source = f"trip {trip.trip_id}"
        if trip.start and trip.end and trip.start in text and trip.end in text:
            for i, line in enumerate(lines):
                window = lines[max(0, i - PAIR_WINDOW):i + PAIR_WINDOW + 1]
                if trip.start in line and any(trip.end in w for w in window):
                    findings.append(Finding(
                        path, i + 1, "trip dates",
                        f"{trip.start}..{trip.end}", source))
        for place, pattern in trip.places:
            if place not in lowered:
                continue
            for match in pattern.finditer(text):
                i = bisect.bisect_right(line_starts, match.start()) - 1
                shared = months_near(i) & trip.months
                if shared:
                    year, month = min(shared)
                    findings.append(Finding(
                        path, i + 1, "trip place + month",
                        f"{match.group()} {year}-{month:02d}", source))
    return findings


def scan_text(path: str, text: str, needles: Needles) -> list[Finding]:
    lines = text.splitlines()
    amounts_by_line = [_line_amounts(line) for line in lines]
    findings: list[Finding] = []

    # Patterns run once per file, not once per line: a hundred-odd
    # patterns over every line of the repo is millions of regex calls.
    line_starts = [0]
    for line in text.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))
    lowered = text.lower()
    for kind, pattern, literal in needles.patterns:
        if literal is not None and literal not in lowered:
            continue
        for match in pattern.finditer(text):
            n = bisect.bisect_right(line_starts, match.start())
            findings.append(Finding(path, n, kind, match.group(), kind))

    for i, line in enumerate(lines):
        n = i + 1
        tokens = set(_TOKEN.findall(line))
        tokens |= {part for tok in tokens for part in tok.split("-")}
        # Membership per token: `set & dict.keys()` copies every key on
        # every line.
        for token in sorted(t for t in tokens if t in needles.identifiers):
            findings.append(Finding(path, n, "identifier", token,
                                    needles.identifiers[token]))

        for match in _FOUR_DIGITS.finditer(line):
            if match.group() not in needles.last4:
                continue
            before = line[max(0, match.start() - 16):match.start()].lower()
            after = line[match.end():match.end() + 2]
            if after == "__" or any(k in before for k in _CARD_CONTEXT):
                findings.append(Finding(path, n, "card last4", match.group(),
                                        "statement sidecars"))

        for amount, shown in amounts_by_line[i]:
            if amount in needles.amounts:
                findings.append(Finding(path, n, "amount", shown,
                                        needles.amounts[amount]))

        for date in _DATE.findall(line):
            window = amounts_by_line[max(0, i - PAIR_WINDOW):i + PAIR_WINDOW + 1]
            for amount, shown in {a for row in window for a in row}:
                source = needles.dated.get((date, amount))
                if source:
                    findings.append(Finding(path, n, "date + amount",
                                            f"{date} {shown}", source))

    findings += _trip_findings(path, text, lines, line_starts, lowered,
                               needles.trips)
    return findings


def load_allowlist(path: Path) -> list[tuple[str, str]]:
    """Parse ``<path glob> <text>`` lines; text compares case-insensitively."""
    entries = []
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                glob, _, text = line.partition(" ")
                entries.append((glob, text.strip().lower()))
    return entries


def is_allowed(finding: Finding, allowlist: list[tuple[str, str]]) -> bool:
    return any(
        fnmatch.fnmatchcase(finding.path, glob)
        and finding.text.lower() == text
        for glob, text in allowlist
    )


def scan(repo: Path, needles: Needles, allowlist: list[tuple[str, str]],
         rev: str | None = None) -> list[Finding]:
    findings = []
    for path, text in tracked_texts(repo, rev).items():
        findings += [
            f for f in scan_text(path, text, needles)
            if not is_allowed(f, allowlist)
        ]
    return list(dict.fromkeys(findings))


# `git commit --verbose` appends the diff below this line; git drops it,
# and the staged files are the pre-commit scan's job.
_SCISSORS = "# ------------------------ >8 ------------------------"


def scan_message(text: str, needles: Needles,
                 allowlist: list[tuple[str, str]]) -> list[Finding]:
    """Scan a commit message as git will store it, without '#' comments."""
    kept = []
    for line in text.splitlines():
        if line == _SCISSORS:
            break
        if not line.startswith("#"):
            kept.append(line)
    return [
        f for f in dict.fromkeys(scan_text(MESSAGE_PATH, "\n".join(kept),
                                           needles))
        if not is_allowed(f, allowlist)
    ]


def scan_commits(repo: Path, needles: Needles,
                 allowlist: list[tuple[str, str]],
                 rev_args: list[str]) -> list[tuple[str, Finding]]:
    """Scan the message and tree of every commit `git rev-list` lists.

    Each finding is reported once, against the oldest commit that has
    it. A file is read only in the commits that change it.
    """
    shas = _git(repo, "rev-list", "--topo-order", "--reverse",
                *rev_args).decode().split()
    commits = _cat_objects(repo, shas)
    seen_blobs: set[tuple[str, str]] = set()
    reported: dict[tuple[str, str, str], tuple[str, Finding]] = {}
    for sha in shas:
        _headers, _, message = commits[sha].partition(b"\n\n")
        found = scan_message(message.decode("utf-8", errors="replace"),
                             needles, allowlist)
        blobs = {pair for pair in _tree_blobs(repo, sha).items()
                 if pair not in seen_blobs}
        seen_blobs |= blobs
        texts = _blob_texts(repo, (blob for _, blob in blobs))
        for path, blob in sorted(blobs):
            if blob in texts:
                found += [f for f in scan_text(path, texts[blob], needles)
                          if not is_allowed(f, allowlist)]
        for f in found:
            reported.setdefault((f.path, f.kind, f.text.lower()), (sha, f))
    return list(reported.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="housebook-leak-scan",
        description="Report real workspace data found in tracked files "
                    "and commit messages.",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--rev",
        help="scan this commit's tree instead of the index (staged content)",
    )
    target.add_argument(
        "--message", metavar="FILE",
        help="scan a commit message file (the commit-msg hook)",
    )
    target.add_argument(
        "--commits", nargs=argparse.REMAINDER, metavar="REV",
        help="scan the message and tree of every commit that `git rev-list "
             "REV...` lists, e.g. `--commits origin/main..HEAD` (the "
             "pre-push hook); everything after it goes to git rev-list",
    )
    args = parser.parse_args(argv)
    if args.commits == []:
        parser.error("--commits needs git rev-list arguments")

    load_dotenv()
    if not os.environ.get("HOUSEBOOK_WORKSPACE_DIR", "").strip():
        print("housebook-leak-scan: no workspace configured, so there is no "
              "real data to compare against. Skipped.", file=sys.stderr)
        return 0
    from housebook.config import settings

    needles = load_needles(settings.WORKSPACE_DIR, Path(settings.DB_PATH))
    allowlist_path = settings.CONFIG_DIR / "leak-scan-allow.txt"
    allowlist = load_allowlist(allowlist_path)
    for note in needles.notes:
        print(f"housebook-leak-scan: note: {note}", file=sys.stderr)

    if args.commits:
        located = scan_commits(settings.PROJECT_ROOT, needles, allowlist,
                               args.commits)
        target_name = "those commits"
    elif args.message:
        text = Path(args.message).read_text(encoding="utf-8",
                                            errors="replace")
        located = [(None, f) for f in scan_message(text, needles, allowlist)]
        target_name = "the commit message"
    else:
        located = [(None, f) for f in scan(settings.PROJECT_ROOT, needles,
                                           allowlist, args.rev)]
        target_name = args.rev or "index"
    for sha, f in located:
        where = f"{sha[:10]} " if sha else ""
        print(f"{where}{f.path}:{f.line}: {f.kind}: {f.text}  [{f.source}]")
    if located:
        print(f"\n{len(located)} finding(s) in {target_name}. Replace real "
              f"values with invented ones, and describe a problem's shape "
              f"rather than the household event behind it (AGENTS.md, "
              f"'Fictitious Data Only'). List intentional ones in "
              f"{allowlist_path} as '<path glob> <text>'; a commit "
              f"message's path is {MESSAGE_PATH}.", file=sys.stderr)
        return 1
    print(f"No workspace data found in {target_name}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
