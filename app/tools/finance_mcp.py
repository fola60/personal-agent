#!/usr/bin/env python3
"""
Finance tool handlers — transaction management for the personal agent.

Tools:
  import_csv  – parse an AIB bank CSV export and store transactions

Categorisation: All transactions are classified by gpt-4o-mini in batches
of 25 for optimal accuracy and token efficiency.
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

# Default categories — only used as fallback if DB is unreachable
_DEFAULT_CATEGORIES = [
    "groceries", "dining", "transport", "rent", "utilities",
    "entertainment", "health", "shopping", "subscriptions",
    "income", "transfer", "savings", "education", "other",
]

AI_BATCH_SIZE = 25  # transactions per AI call — balances accuracy vs tokens


async def _fetch_categories() -> list[str]:
    """Fetch all category names from the database."""
    from app.models import Category
    try:
        async with _Session() as db:
            result = await db.execute(select(Category.name))
            cats = [r[0] for r in result.all()]
            return cats if cats else _DEFAULT_CATEGORIES
    except Exception as e:
        logger.warning("Failed to fetch categories from DB, using defaults: %s", e)
        return _DEFAULT_CATEGORIES


# ---------------------------------------------------------------------------
# AIB CSV parsing helpers
# ---------------------------------------------------------------------------

# Common AIB description prefixes to clean up
_PREFIX_RE = re.compile(r"^(VDP-|VDC-|VCR-|DD-|SO-|TFR-|CHQ-)")


def _clean_description(raw: str) -> str:
    """Remove AIB prefix codes from descriptions."""
    return _PREFIX_RE.sub("", raw).strip()


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
                "category": "unclassified",
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
# AI batch categorisation — classifies ALL transactions via gpt-4o-mini
# ---------------------------------------------------------------------------

async def _ai_categorise_batch(items: list[dict]) -> dict[int, str]:
    """
    Send a single batch of transactions to gpt-4o-mini for classification.

    Args:
        items: list of dicts with keys: index, description, amount

    Returns:
        Mapping of {original_index: category}
    """
    if not items:
        return {}

    categories = await _fetch_categories()
    lines = [f"{it['index']}|{it['description']}|{it['amount']}" for it in items]

    prompt = (
        "Categorise each bank transaction below into exactly one category.\n\n"
        f"Valid categories: {', '.join(categories)}\n\n"
        "Transactions (format: index|description|amount):\n"
        + "\n".join(lines)
        + "\n\nRespond with ONLY a JSON object mapping index (as string) to category. "
        "Example: {\"0\": \"groceries\", \"1\": \"dining\"}"
    )

    try:
        client = _get_openai()
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=2000,
        )
        content = resp.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        result = json.loads(content)
        return {
            int(k): v
            for k, v in result.items()
            if v in categories
        }
    except Exception as e:
        logger.warning("AI categorisation batch failed: %s", e)
        return {}


async def _ai_categorise_all(parsed: list[dict]) -> None:
    """
    Classify every transaction in *parsed* via AI, mutating each
    dict's 'category' in place.  Processes in batches of AI_BATCH_SIZE.
    """
    all_items = [
        {"index": i, "description": row["description"], "amount": str(row["amount"])}
        for i, row in enumerate(parsed)
    ]

    classified = 0
    for start in range(0, len(all_items), AI_BATCH_SIZE):
        batch = all_items[start : start + AI_BATCH_SIZE]
        results = await _ai_categorise_batch(batch)
        for item in batch:
            idx = item["index"]
            if idx in results:
                parsed[idx]["category"] = results[idx]
                classified += 1
            else:
                parsed[idx]["category"] = "other"  # fallback

    logger.info(
        "AI classified %d/%d transactions in %d batch(es)",
        classified, len(parsed),
        (len(parsed) + AI_BATCH_SIZE - 1) // AI_BATCH_SIZE,
    )


# ---------------------------------------------------------------------------
# TransactionTip tool handlers
# ---------------------------------------------------------------------------

async def _fetch_tips(phone_number: str) -> list[dict]:
    """Fetch all tips for a user."""
    from app.models import TransactionTip
    async with _Session() as db:
        result = await db.execute(
            select(TransactionTip.pattern, TransactionTip.category)
            .where(TransactionTip.phone_number == phone_number)
        )
        return [dict(pattern=r[0], category=r[1]) for r in result.all()]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

async def call_tool(name: str, arguments: dict) -> str:
    from app.models import Budget, Category, Transaction, TransactionTip

    async with _Session() as db:
        if name == "add_category":
            cat_name = arguments.get("name", "").strip().lower()
            if not cat_name:
                return "✗ Category name is required."

            existing = await db.execute(
                select(Category).where(Category.name == cat_name)
            )
            if existing.scalar_one_or_none():
                return f"Category '{cat_name}' already exists."

            db.add(Category(name=cat_name, is_default=False))
            await db.commit()
            return f"✓ Category '{cat_name}' added."

        elif name == "list_categories":
            result = await db.execute(select(Category).order_by(Category.name))
            cats = result.scalars().all()
            if not cats:
                return "No categories found."
            lines = []
            for c in cats:
                tag = " (default)" if c.is_default else " (custom)"
                lines.append(f"  • {c.name}{tag}")
            return f"Categories ({len(cats)}):\n" + "\n".join(lines)

        elif name == "remove_category":
            cat_name = arguments.get("name", "").strip().lower()
            if not cat_name:
                return "✗ Category name is required."

            result = await db.execute(
                select(Category).where(Category.name == cat_name)
            )
            cat = result.scalar_one_or_none()
            if not cat:
                return f"✗ Category '{cat_name}' not found."
            if cat.is_default:
                return f"✗ Cannot remove default category '{cat_name}'."

            await db.delete(cat)
            await db.commit()
            return f"✓ Category '{cat_name}' removed."

        elif name == "set_budget":
            cat_name = arguments.get("category", "").strip().lower()
            amount_str = arguments.get("amount", "")
            phone_number = arguments.get("phone_number", "")

            if not cat_name:
                return "\u2717 Category is required."
            if not phone_number:
                return "\u2717 phone_number is required."

            try:
                budget_amount = Decimal(str(amount_str))
            except (InvalidOperation, ValueError):
                return f"\u2717 Invalid amount: {amount_str}"

            # Find the category
            result = await db.execute(
                select(Category).where(Category.name == cat_name)
            )
            cat = result.scalar_one_or_none()
            if not cat:
                return f"\u2717 Category '{cat_name}' not found. Use finance_list_categories to see available categories."

            # Upsert: check if budget exists for this category
            result = await db.execute(
                select(Budget).where(Budget.category_id == cat.id)
            )
            existing = result.scalar_one_or_none()
            if existing:
                existing.amount = budget_amount
                existing.phone_number = phone_number
            else:
                db.add(Budget(
                    category_id=cat.id,
                    phone_number=phone_number,
                    amount=budget_amount,
                ))
            await db.commit()
            return f"\u2713 Budget for '{cat_name}' set to \u20ac{budget_amount:.2f}/month."

        elif name == "list_budgets":
            result = await db.execute(
                select(Budget, Category.name)
                .join(Category, Budget.category_id == Category.id)
                .order_by(Category.name)
            )
            rows = result.all()
            if not rows:
                return "No budgets set. Use finance_set_budget to create one."
            lines = []
            for budget, cat_name in rows:
                lines.append(f"  \u2022 {cat_name}: \u20ac{budget.amount:.2f}/month")
            return f"Budgets ({len(rows)}):\n" + "\n".join(lines)

        elif name == "remove_budget":
            cat_name = arguments.get("category", "").strip().lower()
            if not cat_name:
                return "\u2717 Category is required."

            result = await db.execute(
                select(Budget)
                .join(Category, Budget.category_id == Category.id)
                .where(Category.name == cat_name)
            )
            budget = result.scalar_one_or_none()
            if not budget:
                return f"\u2717 No budget found for '{cat_name}'."

            await db.delete(budget)
            await db.commit()
            return f"\u2713 Budget for '{cat_name}' removed."

        elif name == "check_budgets":
            phone_number = arguments.get("phone_number", "")
            if not phone_number:
                return "\u2717 phone_number is required."

            # Get all budgets
            result = await db.execute(
                select(Budget, Category.name)
                .join(Category, Budget.category_id == Category.id)
                .order_by(Category.name)
            )
            budget_rows = result.all()
            if not budget_rows:
                return "No budgets set."

            # Get current month spending per category
            now = datetime.now()
            month_start = now.replace(day=1).date()

            from sqlalchemy import func as sa_func
            result = await db.execute(
                select(Transaction.category, sa_func.sum(sa_func.abs(Transaction.amount)))
                .where(
                    Transaction.phone_number == phone_number,
                    Transaction.transaction_type == "debit",
                    Transaction.date >= month_start,
                )
                .group_by(Transaction.category)
            )
            spending = {row[0]: row[1] for row in result.all()}

            lines = []
            for budget, cat_name in budget_rows:
                spent = spending.get(cat_name, Decimal("0"))
                remaining = budget.amount - spent
                pct = (spent / budget.amount * 100) if budget.amount > 0 else Decimal("0")
                status = "\u2705" if remaining >= 0 else "\u26a0\ufe0f"
                lines.append(
                    f"  {status} {cat_name}: \u20ac{spent:.2f} / \u20ac{budget.amount:.2f} "
                    f"({pct:.0f}%) — \u20ac{abs(remaining):.2f} {'left' if remaining >= 0 else 'over'}"
                )

            month_label = now.strftime("%B %Y")
            return f"Budget report — {month_label}:\n" + "\n".join(lines)

        elif name == "import_csv":
            csv_text = arguments.get("csv_text", "")
            phone_number = arguments.get("phone_number", "")

            if not csv_text.strip():
                return "✗ No CSV data provided."
            if not phone_number:
                return "✗ phone_number is required."

            parsed = _parse_aib_csv(csv_text)
            if not parsed:
                return "✗ No transactions found in the CSV data."

            # Apply user tips before AI categorisation
            tips = await _fetch_tips(phone_number)
            _apply_tips(parsed, phone_number, tips)

            # Classify remaining transactions via AI in batches
            await _ai_categorise_all(parsed)

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

        elif name == "add_tip":
            phone_number = arguments.get("phone_number", "")
            pattern = arguments.get("pattern", "").strip()
            category = arguments.get("category", "").strip().lower()
            if not phone_number or not pattern or not category:
                return "✗ phone_number, pattern, and category are required."

            # Validate category
            cats = await _fetch_categories()
            if category not in cats:
                return f"✗ Category '{category}' not found. Use finance_list_categories to see available categories."

            db.add(TransactionTip(
                phone_number=phone_number,
                pattern=pattern,
                category=category,
            ))
            await db.commit()
            return f"✓ Tip added: '{pattern}' → {category}"

        elif name == "list_tips":
            phone_number = arguments.get("phone_number", "")
            if not phone_number:
                return "✗ phone_number is required."
            tips = await _fetch_tips(phone_number)
            if not tips:
                return "No tips found. Use finance_add_tip to add one."
            lines = [f"  • '{t['pattern']}' → {t['category']}" for t in tips]
            return f"Tips ({len(tips)}):\n" + "\n".join(lines)

        elif name == "remove_tip":
            phone_number = arguments.get("phone_number", "")
            pattern = arguments.get("pattern", "").strip()
            if not phone_number or not pattern:
                return "✗ phone_number and pattern are required."
            result = await db.execute(
                select(TransactionTip)
                .where(TransactionTip.phone_number == phone_number, TransactionTip.pattern == pattern)
            )
            tip = result.scalar_one_or_none()
            if not tip:
                return f"✗ Tip '{pattern}' not found."
            await db.delete(tip)
            await db.commit()
            return f"✓ Tip '{pattern}' removed."

        else:
            text = f"Unknown tool: {name}"

    return text


def _apply_tips(transactions: list[dict], phone_number: str, tips: list[dict]) -> None:
    """Apply user tips to transactions, setting category if pattern matches."""
    for txn in transactions:
        desc = txn["description"].lower()
        for tip in tips:
            if tip["pattern"].lower() in desc:
                txn["category"] = tip["category"]
                break
