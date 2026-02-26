#!/usr/bin/env python3
"""
Finance tool handlers — transaction management for the personal agent.

Tools:
  import_csv  – parse an AIB bank CSV export and store transactions

Categorisation pipeline:
  1. Keyword-based matching (free, instant, ~60-70% coverage)
  2. AI batch classification via gpt-4o-mini for remaining "other" rows (~$0.003)
"""
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Standalone DB engine
# ---------------------------------------------------------------------------

_db_url = os.getenv("DATABASE_URL", "")
if _db_url.startswith("postgresql://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

_engine = create_async_engine(_db_url, pool_pre_ping=True)
_Session = async_sessionmaker(bind=_engine, class_=AsyncSession, expire_on_commit=False)

# ---------------------------------------------------------------------------
# OpenAI client (lazy)
# ---------------------------------------------------------------------------

_openai_client: AsyncOpenAI | None = None


def _get_openai() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI()
    return _openai_client


# ---------------------------------------------------------------------------
# Valid categories
# ---------------------------------------------------------------------------

VALID_CATEGORIES = [
    "groceries", "dining", "transport", "rent", "utilities",
    "entertainment", "health", "shopping", "subscriptions",
    "income", "transfer", "savings", "education", "other",
]

# ---------------------------------------------------------------------------
# Standalone DB engine
# ---------------------------------------------------------------------------

_db_url = os.getenv("DATABASE_URL", "")
if _db_url.startswith("postgresql://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

_engine = create_async_engine(_db_url, pool_pre_ping=True)
_Session = async_sessionmaker(bind=_engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# AIB CSV parsing helpers
# ---------------------------------------------------------------------------

# Common AIB description prefixes to clean up
_PREFIX_RE = re.compile(r"^(VDP-|VDC-|VCR-|DD-|SO-|TFR-|CHQ-)")

# Known merchant → category mappings
_CATEGORY_MAP: dict[str, str] = {
    "aldi": "groceries",
    "lidl": "groceries",
    "tesco": "groceries",
    "dunnes": "groceries",
    "supervalu": "groceries",
    "centra": "groceries",
    "spar": "groceries",
    "spotify": "subscriptions",
    "netflix": "subscriptions",
    "disney": "subscriptions",
    "youtube": "subscriptions",
    "amazon prime": "subscriptions",
    "flyefit": "subscriptions",
    "anthropic": "subscriptions",
    "openai": "subscriptions",
    "chatgpt": "subscriptions",
    "amzn mktp": "shopping",
    "amazon": "shopping",
    "irish rail": "transport",
    "leap card": "transport",
    "dublin bus": "transport",
    "luas": "transport",
    "uber": "transport",
    "bolt": "transport",
    "freenow": "transport",
    "just eat": "dining",
    "deliveroo": "dining",
    "mcdonald": "dining",
    "starbucks": "dining",
    "costa": "dining",
    "eir": "utilities",
    "virgin media": "utilities",
    "electric ireland": "utilities",
    "bord gais": "utilities",
    "three.ie": "utilities",
}


def _clean_description(raw: str) -> str:
    """Remove AIB prefix codes from descriptions."""
    return _PREFIX_RE.sub("", raw).strip()


def _guess_category(description: str) -> str:
    """Guess a spending category from the transaction description."""
    desc_lower = description.lower()
    for keyword, category in _CATEGORY_MAP.items():
        if keyword in desc_lower:
            return category
    return "other"


def _parse_amount(value: str) -> Decimal | None:
    """Parse a decimal amount string, returning None if empty/invalid."""
    value = value.strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _make_external_id(account: str, date_str: str, description: str, amount: str) -> str:
    """Create a stable dedup key from transaction fields."""
    raw = f"{account}|{date_str}|{description}|{amount}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _parse_aib_csv(csv_text: str) -> list[dict]:
    """
    Parse AIB tab-separated CSV export into a list of transaction dicts.

    Handles multi-row transactions: rows where both debit and credit
    amounts are empty or 0.00 are treated as metadata for the previous
    transaction and merged into its description.

    Returns list of dicts with keys:
        date, description, raw_description, amount, balance,
        transaction_type, category, external_id
    """
    lines = csv_text.strip().splitlines()
    if not lines:
        return []

    # Detect separator (tab or comma)
    first_line = lines[0]
    sep = "\t" if "\t" in first_line else ","

    # Skip header if present
    header_lower = first_line.lower()
    if "posted account" in header_lower or "description" in header_lower:
        lines = lines[1:]

    transactions: list[dict] = []
    current_txn: dict | None = None

    for line in lines:
        cols = [c.strip() for c in line.split(sep)]
        if len(cols) < 6:
            continue

        account = cols[0]
        date_str = cols[1]
        description = cols[2]
        debit_str = cols[3]
        credit_str = cols[4]
        balance_str = cols[5] if len(cols) > 5 else ""

        debit = _parse_amount(debit_str)
        credit = _parse_amount(credit_str)

        # Determine if this is a real transaction or a metadata row
        has_real_amount = (debit is not None and debit > 0) or (credit is not None and credit > 0)

        if has_real_amount:
            # Save previous transaction if exists
            if current_txn is not None:
                transactions.append(current_txn)

            # Parse date (DD/MM/YY format)
            try:
                txn_date = datetime.strptime(date_str, "%d/%m/%y").date()
            except ValueError:
                try:
                    txn_date = datetime.strptime(date_str, "%d/%m/%Y").date()
                except ValueError:
                    logger.warning("Skipping row with unparseable date: %s", date_str)
                    continue

            if debit and debit > 0:
                amount = -debit  # expenses are negative
                txn_type = "debit"
            else:
                amount = credit  # income is positive
                txn_type = "credit"

            clean_desc = _clean_description(description)
            balance = _parse_amount(balance_str)
            ext_id = _make_external_id(account, date_str, description, str(amount))

            current_txn = {
                "date": txn_date,
                "description": clean_desc,
                "raw_description": description,
                "amount": amount,
                "balance": balance,
                "transaction_type": txn_type,
                "category": _guess_category(clean_desc),
                "external_id": ext_id,
            }
        else:
            # Metadata row — merge into current transaction
            if current_txn is not None:
                # Append meaningful metadata to description
                if description and not description.startswith("TxnDate:"):
                    current_txn["raw_description"] += f" | {description}"
                # Capture balance if this row has it
                bal = _parse_amount(balance_str)
                if bal is not None and bal > 0:
                    current_txn["balance"] = bal

    # Don't forget the last transaction
    if current_txn is not None:
        transactions.append(current_txn)

    return transactions


# ---------------------------------------------------------------------------
# AI batch categorisation (for rows the keyword matcher missed)
# ---------------------------------------------------------------------------

async def _ai_categorise_batch(uncategorised: list[dict]) -> dict[int, str]:
    """
    Send uncategorised transactions to gpt-4o-mini in a single batch.

    Args:
        uncategorised: list of dicts with keys: index, description, amount

    Returns:
        Mapping of {original_index: category}
    """
    if not uncategorised:
        return {}

    lines = []
    for item in uncategorised:
        lines.append(f"{item['index']}|{item['description']}|{item['amount']}")

    prompt = (
        "Categorise each bank transaction below into exactly one category.\n\n"
        f"Valid categories: {', '.join(VALID_CATEGORIES)}\n\n"
        "Transactions (format: index|description|amount):\n"
        + "\n".join(lines)
        + "\n\nRespond with ONLY a JSON object mapping index (as string) to category. "
        "Example: {\"0\": \"groceries\", \"1\": \"dining\"}"
    )

    try:
        client = _get_openai()
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=1000,
        )
        content = response.choices[0].message.content.strip()

        # Handle markdown code blocks
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        result = json.loads(content)
        return {
            int(k): v
            for k, v in result.items()
            if v in VALID_CATEGORIES
        }
    except Exception as e:
        logger.warning("AI categorisation failed, keeping keyword categories: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

async def call_tool(name: str, arguments: dict) -> str:
    from app.models import Transaction

    async with _Session() as db:
        if name == "import_csv":
            csv_text = arguments.get("csv_text", "")
            phone_number = arguments.get("phone_number", "")

            if not csv_text.strip():
                return "✗ No CSV data provided."
            if not phone_number:
                return "✗ phone_number is required."

            parsed = _parse_aib_csv(csv_text)
            if not parsed:
                return "✗ No transactions found in the CSV data."

            # ── Phase 1: keyword categorisation (free, instant) ──
            # Already done inside _parse_aib_csv via _guess_category

            # ── Phase 2: AI categorisation for "other" rows ──
            uncategorised = [
                {"index": i, "description": row["description"], "amount": str(row["amount"])}
                for i, row in enumerate(parsed)
                if row["category"] == "other" and row["transaction_type"] == "debit"
            ]
            if uncategorised:
                ai_results = await _ai_categorise_batch(uncategorised)
                for item in uncategorised:
                    idx = item["index"]
                    if idx in ai_results:
                        parsed[idx]["category"] = ai_results[idx]
                logger.info(
                    "AI categorised %d/%d uncategorised transactions",
                    len(ai_results), len(uncategorised),
                )

            # Check which external_ids already exist to avoid duplicates
            ext_ids = [t["external_id"] for t in parsed if t.get("external_id")]
            existing_ids: set[str] = set()
            if ext_ids:
                result = await db.execute(
                    select(Transaction.external_id).where(
                        Transaction.external_id.in_(ext_ids)
                    )
                )
                existing_ids = {r[0] for r in result.all() if r[0]}

            imported = 0
            skipped = 0
            for txn in parsed:
                if txn["external_id"] in existing_ids:
                    skipped += 1
                    continue

                db.add(Transaction(
                    phone_number=phone_number,
                    date=txn["date"],
                    description=txn["description"],
                    amount=txn["amount"],
                    balance=txn["balance"],
                    category=txn["category"],
                    transaction_type=txn["transaction_type"],
                    source="csv",
                    raw_description=txn["raw_description"],
                    external_id=txn["external_id"],
                ))
                imported += 1

            await db.commit()

            # Build summary
            total_debits = sum(t["amount"] for t in parsed if t["transaction_type"] == "debit")
            total_credits = sum(t["amount"] for t in parsed if t["transaction_type"] == "credit")

            # Category breakdown for imported
            cats: dict[str, Decimal] = {}
            for t in parsed:
                if t["external_id"] not in existing_ids and t["transaction_type"] == "debit":
                    cat = t["category"]
                    cats[cat] = cats.get(cat, Decimal("0")) + abs(t["amount"])

            cat_lines = ""
            if cats:
                sorted_cats = sorted(cats.items(), key=lambda x: x[1], reverse=True)
                cat_lines = "\n\nSpending by category:\n" + "\n".join(
                    f"  • {cat}: €{amt:.2f}" for cat, amt in sorted_cats
                )

            text = (
                f"✓ CSV imported: {imported} new transaction(s), {skipped} duplicate(s) skipped.\n"
                f"  Total spending:  €{abs(total_debits):.2f}\n"
                f"  Total income:    €{total_credits:.2f}\n"
                f"  Date range:      {parsed[-1]['date']} → {parsed[0]['date']}"
                f"{cat_lines}"
            )

        else:
            text = f"Unknown tool: {name}"

    return text
