"""Find real workspace data in the repository's tracked files.

A denylist only catches strings someone already knows are private. The
leaks that actually happen are copies: a real row becomes a fixture or
an SOP example, its people and companies are renamed, and its numbers
stay. This scanner reads the live workspace read-only and reports any
tracked text that reproduces it:

- identifiers (Amazon order IDs, bank reference codes, claim, ticket
  and agreement numbers) anywhere;
- card last4 digits next to card context (``last4``, ``__1234__``, ``*``);
- a date and an amount from the same real record within a few lines;
- distinctive standalone amounts from medical, tax, statement-balance
  and off-ledger records;
- names: the PII denylist, home location, patients, cardholders,
  passengers, providers and tax issuers.

It scans the git index (what the next commit contains) by default, or
the tree of any commit with ``--rev``. Findings that are intentional,
such as the author's name in LICENSE, go in
``$WORKSPACE/config/leak-scan-allow.txt`` as ``<path glob> <text>``.
"""

from __future__ import annotations

import argparse
import bisect
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

# Sidecar/metadata keys whose values identify one real document.
IDENTIFIER_KEYS = {
    "account_number", "agreement_number", "amazon_order_id", "claim_id",
    "member_id", "policy_number", "ticket_number", "trip_code",
}
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
                elif key in ("balances", "payment"):
                    self.walk(value, source, amounts=True)
                    continue
                self.walk(value, source, amounts)
        elif isinstance(node, list):
            for value in node:
                self.walk(value, source, amounts)
        elif amounts and isinstance(node, (int, float)):
            self.amount(node, source)

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
    finally:
        conn.close()


# ── tracked text ────────────────────────────────────────────────────


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin, capture_output=True, check=True,
    ).stdout


def tracked_texts(repo: Path, rev: str | None = None) -> dict[str, str]:
    """Return {path: text} for the index (default) or a commit's tree.

    Binary blobs, submodules and generated/vendored files are skipped.
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
    blobs = {
        path: sha for path, sha in blobs.items()
        if not path.startswith(SKIPPED_PREFIXES)
    }

    shas = list(dict.fromkeys(blobs.values()))
    batch = _git(repo, "cat-file", "--batch",
                 stdin=("\n".join(shas) + "\n").encode())
    contents: dict[str, bytes] = {}
    pos = 0
    for sha in shas:
        header_end = batch.index(b"\n", pos)
        size = int(batch[pos:header_end].split()[2])
        contents[sha] = batch[header_end + 1:header_end + 1 + size]
        pos = header_end + 1 + size + 1

    texts = {}
    for path, sha in sorted(blobs.items()):
        data = contents[sha]
        if b"\0" not in data:
            texts[path] = data.decode("utf-8", errors="replace")
    return texts


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="housebook-leak-scan",
        description="Report real workspace data found in tracked files.",
    )
    parser.add_argument(
        "--rev",
        help="scan this commit's tree instead of the index (staged content)",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    if not os.environ.get("HOUSEBOOK_WORKSPACE_DIR", "").strip():
        print("housebook-leak-scan: no workspace configured, so there is no "
              "real data to compare against. Skipped.", file=sys.stderr)
        return 0
    from housebook.config import settings

    needles = load_needles(settings.WORKSPACE_DIR, Path(settings.DB_PATH))
    allowlist_path = settings.CONFIG_DIR / "leak-scan-allow.txt"
    findings = scan(settings.PROJECT_ROOT, needles,
                    load_allowlist(allowlist_path), args.rev)
    for f in findings:
        print(f"{f.path}:{f.line}: {f.kind}: {f.text}  [{f.source}]")
    target = args.rev or "index"
    if findings:
        print(f"\n{len(findings)} finding(s) in {target}. Replace real "
              f"values with invented ones; list intentional ones in "
              f"{allowlist_path} as '<path glob> <text>'.", file=sys.stderr)
        return 1
    print(f"No workspace data found in {target}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
