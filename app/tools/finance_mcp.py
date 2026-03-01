import httpx
import json
import logging
import os
from app.models import AIBUser
from datetime import datetime, timedelta, timezone
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


# --- TrueLayer Token Refresh Logic ---
async def _get_valid_token(phone_number: str) -> str:
    """Return a valid access token for the user, refreshing if expired."""
    async with _Session() as db:
        result = await db.execute(select(AIBUser).where(AIBUser.phone_number == phone_number))
        user = result.scalar_one_or_none()
        if not user:
            raise Exception("No TrueLayer credentials found for user.")
        now = datetime.now(timezone.utc)
        if user.expires_at > now:
            return user.access_token
        # Refresh token
        TRUELAYER_CLIENT_ID = os.getenv("TRUELAYER_CLIENT_ID", "")
        TRUELAYER_CLIENT_SECRET = os.getenv("TRUELAYER_CLIENT_SECRET", "")
        url = "https://auth.truelayer.com/connect/token"
        data = {
            "grant_type": "refresh_token",
            "refresh_token": user.refresh_token,
            "client_id": TRUELAYER_CLIENT_ID,
            "client_secret": TRUELAYER_CLIENT_SECRET,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, data=data)
            resp.raise_for_status()
            result = resp.json()
        user.access_token = result["access_token"]
        user.refresh_token = result.get("refresh_token", user.refresh_token)
        expires_in = result.get("expires_in", 3600)
        user.expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        await db.commit()
        return user.access_token

# --- TrueLayer API Helpers ---
async def _truelayer_get(endpoint: str, access_token: str, params=None):
    url = f"https://api.truelayer.com/{endpoint}"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()


# --- TrueLayer Transaction Import ---
async def import_transactions_from_truelayer(phone_number: str, access_token: str) -> int:
    """
    Import transactions from TrueLayer into the local database.
    Called automatically when a user connects their bank account.
    
    Returns the count of newly imported transactions.
    """
    import hashlib
    import json
    import logging
    from decimal import Decimal
    from app.models import Transaction
    
    logger = logging.getLogger(__name__)
    
    # Fetch accounts
    try:
        accounts = await _truelayer_get("data/v1/accounts", access_token)
    except Exception as e:
        logger.error(f"[import_transactions] Failed to fetch accounts for {phone_number}: {e}")
        return 0
    
    if not accounts.get("results"):
        logger.warning(f"[import_transactions] No accounts found for {phone_number}")
        return 0
    
    imported_count = 0
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=90)).isoformat()  # Last 90 days
    
    async with _Session() as db:
        for account in accounts["results"]:
            account_id = account["account_id"]
            
            try:
                txns_response = await _truelayer_get(
                    f"data/v1/accounts/{account_id}/transactions",
                    access_token,
                    params={"from": since}
                )
            except Exception as e:
                logger.error(f"[import_transactions] Failed to fetch transactions for account {account_id}: {e}")
                continue
            
            txns = txns_response.get("results", [])
            parsed_txns = []
            
            for txn in txns:
                # Create unique external_id from TrueLayer transaction
                ext_id_raw = f"tl|{account_id}|{txn.get('transaction_id', '')}|{txn.get('timestamp', '')}"
                ext_id = hashlib.sha256(ext_id_raw.encode()).hexdigest()[:32]
                
                # Check if already exists
                existing = await db.execute(
                    select(Transaction).where(Transaction.external_id == ext_id)
                )
                if existing.scalar_one_or_none():
                    continue
                
                # Parse transaction data
                try:
                    txn_date = datetime.fromisoformat(txn["timestamp"].replace("Z", "+00:00")).date()
                except (KeyError, ValueError):
                    txn_date = now.date()
                
                amount_data = txn.get("amount", {})
                amount_value = Decimal(str(amount_data.get("value", 0)))
                
                txn_type = txn.get("transaction_type", "debit").lower()
                if txn_type == "debit":
                    amount_value = -abs(amount_value)  # Expenses are negative
                else:
                    amount_value = abs(amount_value)  # Income is positive
                
                description = txn.get("description", "Unknown")
                
                parsed_txns.append({
                    "date": txn_date,
                    "description": description,
                    "raw_description": json.dumps(txn),
                    "amount": amount_value,
                    "balance": None,
                    "transaction_type": txn_type,
                    "category": "unclassified",
                    "external_id": ext_id,
                })
            
            # AI categorize the batch
            if parsed_txns:
                await _ai_categorise_all(parsed_txns)
                
                # Insert into database
                for txn_data in parsed_txns:
                    new_txn = Transaction(
                        phone_number=phone_number,
                        date=txn_data["date"],
                        description=txn_data["description"],
                        raw_description=txn_data["raw_description"],
                        amount=txn_data["amount"],
                        balance=txn_data["balance"],
                        transaction_type=txn_data["transaction_type"],
                        category=txn_data["category"],
                        source="truelayer",
                        external_id=txn_data["external_id"],
                    )
                    db.add(new_txn)
                    imported_count += 1
                
                await db.commit()
    
    logger.info(f"[import_transactions] Imported {imported_count} transactions for {phone_number}")
    return imported_count


# --- Recurring Transaction Helpers ---
def _normalize_description(desc: str) -> str:
    """
    Normalize a transaction description for pattern matching.
    Removes numbers, special chars, lowercases, and strips whitespace.
    """
    import re
    # Remove numbers (like dates, amounts, reference numbers)
    normalized = re.sub(r'\d+', '', desc)
    # Remove special characters
    normalized = re.sub(r'[^\w\s]', '', normalized)
    # Lowercase and strip
    return ' '.join(normalized.lower().split())


async def _check_recurring_status(phone_number: str) -> list[dict]:
    """
    Check payment status of all recurring transactions for current month.
    Uses fuzzy description matching to detect if payment was made.
    """
    from app.models import Transaction, RecurringTransaction
    
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1).date()
    
    async with _Session() as db:
        # Get all active recurring transactions
        result = await db.execute(
            select(RecurringTransaction)
            .where(RecurringTransaction.phone_number == phone_number)
            .where(RecurringTransaction.is_active == True)
        )
        recurring = result.scalars().all()
        
        # Get this month's transactions
        txns_result = await db.execute(
            select(Transaction)
            .where(Transaction.phone_number == phone_number)
            .where(Transaction.date >= month_start)
            .where(Transaction.transaction_type == "debit")
        )
        this_month_txns = txns_result.scalars().all()
        
        status_list = []
        for rec in recurring:
            # Fuzzy match: check if any transaction description contains the pattern
            pattern_words = set(rec.description_pattern.split())
            paid = False
            paid_date = None
            paid_amount = None
            
            for txn in this_month_txns:
                txn_normalized = _normalize_description(txn.description)
                txn_words = set(txn_normalized.split())
                
                # Match if at least 50% of pattern words are in transaction
                if pattern_words and len(pattern_words & txn_words) >= len(pattern_words) * 0.5:
                    paid = True
                    paid_date = txn.date.isoformat()
                    paid_amount = float(txn.amount)
                    break
            
            status_list.append({
                "name": rec.description_pattern,
                "expected_amount": float(rec.detected_amount),
                "frequency": rec.frequency,
                "category": rec.category,
                "status": "paid" if paid else "unpaid",
                "paid_date": paid_date,
                "paid_amount": paid_amount,
            })
        
        return status_list


# --- Tool Handlers ---
async def call_tool(name: str, arguments: dict) -> str:
    if name == "finance_getall_transactions":
        phone_number = arguments.get("phone_number")
        token = await _get_valid_token(phone_number)
        # Get accounts
        accounts = await _truelayer_get("data/v1/accounts", token)
        if not accounts["results"]:
            return "No accounts found."
        account_id = accounts["results"][0]["account_id"]
        txns = await _truelayer_get(f"data/v1/accounts/{account_id}/transactions", token)
        return json.dumps(txns["results"])
    elif name == "finance_transactions_recent":
        phone_number = arguments.get("phone_number")
        logger.info(f"[get_recent_transactions] Called for phone_number={phone_number}")
        try:
            token = await _get_valid_token(phone_number)
            logger.info(f"[get_recent_transactions] Valid token obtained for user {phone_number}: {token[:8]}...")
        except Exception as e:
            logger.error(f"[get_recent_transactions] Error getting token for {phone_number}: {e}")
            return f"Error getting token: {e}"
        try:
            accounts = await _truelayer_get("data/v1/accounts", token)
            logger.info(f"[get_recent_transactions] Accounts response: {accounts}")
        except Exception as e:
            logger.error(f"[get_recent_transactions] Error fetching accounts: {e}")
            return f"Error fetching accounts: {e}"
        if not accounts.get("results"):
            logger.warning(f"[get_recent_transactions] No accounts found for user {phone_number}")
            return "No accounts found."
        account_id = accounts["results"][0]["account_id"]
        now = datetime.now(timezone.utc)
        since = (now - timedelta(hours=6)).isoformat()
        logger.info(f"[get_recent_transactions] Fetching transactions since {since} for account {account_id}")
        try:
            txns = await _truelayer_get(f"data/v1/accounts/{account_id}/transactions", token, params={"from": since})
            logger.info(f"[get_recent_transactions] Transactions response: {txns}")
        except Exception as e:
            logger.error(f"[get_recent_transactions] Error fetching transactions: {e}")
            return f"Error fetching transactions: {e}"
        txn_count = len(txns.get("results", []))
        logger.info(f"[get_recent_transactions] Returning {txn_count} transactions for user {phone_number}")
        return json.dumps(txns["results"])
    elif name == "finance_get_balance":
        phone_number = arguments.get("phone_number")
        token = await _get_valid_token(phone_number)
        accounts = await _truelayer_get("data/v1/accounts", token)
        if not accounts["results"]:
            return "No accounts found."
        account_id = accounts["results"][0]["account_id"]
        balance = await _truelayer_get(f"data/v1/accounts/{account_id}/balance", token)
        return json.dumps(balance["results"][0])
    elif name == "finance_get_category":
        phone_number = arguments.get("phone_number")
        category = arguments.get("category")
        token = await _get_valid_token(phone_number)
        accounts = await _truelayer_get("data/v1/accounts", token)
        if not accounts["results"]:
            return "No accounts found."
        account_id = accounts["results"][0]["account_id"]
        txns = await _truelayer_get(f"data/v1/accounts/{account_id}/transactions", token)
        filtered = [t for t in txns["results"] if t.get("transaction_category") == category]
        return json.dumps(filtered)
    elif name == "finance_get_merchant":
        phone_number = arguments.get("phone_number")
        merchant = arguments.get("merchant")
        token = await _get_valid_token(phone_number)
        accounts = await _truelayer_get("data/v1/accounts", token)
        if not accounts["results"]:
            return "No accounts found."
        account_id = accounts["results"][0]["account_id"]
        txns = await _truelayer_get(f"data/v1/accounts/{account_id}/transactions", token)
        filtered = [t for t in txns["results"] if merchant.lower() in t.get("description", "").lower()]
        return json.dumps(filtered)
    elif name == "finance_getby_daterange":
        phone_number = arguments.get("phone_number")
        start_date = arguments.get("start_date")
        end_date = arguments.get("end_date")
        token = await _get_valid_token(phone_number)
        accounts = await _truelayer_get("data/v1/accounts", token)
        if not accounts["results"]:
            return "No accounts found."
        account_id = accounts["results"][0]["account_id"]
        txns = await _truelayer_get(f"data/v1/accounts/{account_id}/transactions", token, params={"from": start_date, "to": end_date})
        return json.dumps(txns["results"])
    elif name == "finance_get_scheduledpayments":
        phone_number = arguments.get("phone_number")
        token = await _get_valid_token(phone_number)
        accounts = await _truelayer_get("data/v1/accounts", token)
        if not accounts["results"]:
            return "No accounts found."
        account_id = accounts["results"][0]["account_id"]
        payments = await _truelayer_get(f"data/v1/accounts/{account_id}/scheduled_payments", token)
        return json.dumps(payments["results"])
    elif name == "finance_get_summary":
        phone_number = arguments.get("phone_number")
        period = arguments.get("period")
        token = await _get_valid_token(phone_number)
        accounts = await _truelayer_get("data/v1/accounts", token)
        if not accounts["results"]:
            return "No accounts found."
        account_id = accounts["results"][0]["account_id"]
        txns = await _truelayer_get(f"data/v1/accounts/{account_id}/transactions", token)
        now = datetime.now(timezone.utc)
        if period == "monthly":
            start = now.replace(day=1).date().isoformat()
        elif period == "weekly":
            start = (now - timedelta(days=now.weekday())).date().isoformat()
        else:
            return "Invalid period."
        filtered = [t for t in txns["results"] if t["timestamp"][:10] >= start]
        total = sum(float(t["amount"]['value']) for t in filtered if t["transaction_type"] == "debit")
        return json.dumps({"period": period, "start": start, "spending": total, "count": len(filtered)})
    elif name == "finance_get_status":
        phone_number = arguments.get("phone_number")
        # For demo, just return current month spending and balance
        token = await _get_valid_token(phone_number)
        accounts = await _truelayer_get("data/v1/accounts", token)
        if not accounts["results"]:
            return "No accounts found."
        account_id = accounts["results"][0]["account_id"]
        txns = await _truelayer_get(f"data/v1/accounts/{account_id}/transactions", token)
        start = datetime.now(timezone.utc).replace(day=1).date().isoformat()
        filtered = [t for t in txns["results"] if t["timestamp"][:10] >= start]
        spent = sum(float(t["amount"]['value']) for t in filtered if t["transaction_type"] == "debit")
        balance = await _truelayer_get(f"data/v1/accounts/{account_id}/balance", token)
        bal = float(balance["results"][0]["current"])
        return json.dumps({"month_spent": spent, "current_balance": bal})
    elif name == "finance_get_income":
        phone_number = arguments.get("phone_number")
        token = await _get_valid_token(phone_number)
        accounts = await _truelayer_get("data/v1/accounts", token)
        if not accounts["results"]:
            return "No accounts found."
        account_id = accounts["results"][0]["account_id"]
        txns = await _truelayer_get(f"data/v1/accounts/{account_id}/transactions", token)
        credits = [t for t in txns["results"] if t["transaction_type"] == "credit"]
        return json.dumps(credits)
    
    # --- Recurring Expense Tools ---
    elif name == "finance_list_recurring":
        phone_number = arguments.get("phone_number")
        from app.models import RecurringTransaction
        async with _Session() as db:
            result = await db.execute(
                select(RecurringTransaction)
                .where(RecurringTransaction.phone_number == phone_number)
                .where(RecurringTransaction.is_active == True)
            )
            recurring = result.scalars().all()
            if not recurring:
                return "No recurring expenses set up yet. Add one with finance_add_recurring."
            items = [{
                "name": r.description_pattern,
                "amount": float(r.detected_amount),
                "frequency": r.frequency,
                "category": r.category,
                "last_paid": r.last_paid_at.isoformat() if r.last_paid_at else None,
            } for r in recurring]
            return json.dumps(items)
    
    elif name == "finance_recurring_status":
        phone_number = arguments.get("phone_number")
        try:
            status = await _check_recurring_status(phone_number)
            if not status:
                return "No recurring expenses to check. Add some first."
            paid = sum(1 for s in status if s["status"] == "paid")
            unpaid = len(status) - paid
            return json.dumps({
                "summary": f"{paid} paid, {unpaid} unpaid this month",
                "details": status
            })
        except Exception as e:
            logger.error(f"[finance_recurring_status] Error: {e}")
            return f"Error checking recurring status: {e}"
    
    elif name == "finance_remove_recurring":
        phone_number = arguments.get("phone_number")
        pattern = arguments.get("pattern", "").strip().lower()
        if not pattern:
            return "✗ Pattern is required."
        from app.models import RecurringTransaction
        async with _Session() as db:
            result = await db.execute(
                select(RecurringTransaction)
                .where(RecurringTransaction.phone_number == phone_number)
                .where(RecurringTransaction.description_pattern == pattern)
            )
            rec = result.scalar_one_or_none()
            if not rec:
                return f"✗ Recurring expense '{pattern}' not found."
            rec.is_active = False
            await db.commit()
            return f"✓ Recurring expense '{pattern}' removed."
    
    elif name == "finance_add_recurring":
        phone_number = arguments.get("phone_number")
        name_arg = arguments.get("name", "").strip()
        amount = arguments.get("amount")
        frequency = arguments.get("frequency", "monthly")
        category = arguments.get("category", "").strip()
        
        if not name_arg:
            return "✗ Name is required."
        if not amount or amount <= 0:
            return "✗ Amount must be a positive number."
        if frequency not in ("weekly", "monthly"):
            return "✗ Frequency must be 'weekly' or 'monthly'."
        if not category:
            return "✗ Category is required. Please ask the user which category this expense belongs to."
        
        # Normalize the name for pattern matching
        pattern = _normalize_description(name_arg)
        if not pattern:
            pattern = name_arg.lower()
        
        from decimal import Decimal
        from app.models import RecurringTransaction
        async with _Session() as db:
            # Check if already exists
            result = await db.execute(
                select(RecurringTransaction)
                .where(RecurringTransaction.phone_number == phone_number)
                .where(RecurringTransaction.description_pattern == pattern)
            )
            existing = result.scalar_one_or_none()
            
            if existing:
                # Reactivate and update if exists
                existing.is_active = True
                existing.detected_amount = Decimal(str(amount))
                existing.frequency = frequency
                existing.category = category
                await db.commit()
                return f"✓ Updated recurring expense '{name_arg}' (€{amount:.2f} {frequency}, {category})."
            
            # Create new
            db.add(RecurringTransaction(
                phone_number=phone_number,
                description_pattern=pattern,
                detected_amount=Decimal(str(amount)),
                frequency=frequency,
                category=category,
                is_active=True,
            ))
            await db.commit()
            return f"✓ Added recurring expense '{name_arg}' (€{amount:.2f} {frequency}, {category})."
    
    elif name == "finance_sync_transactions":
        phone_number = arguments.get("phone_number")
        try:
            token = await _get_valid_token(phone_number)
            count = await import_transactions_from_truelayer(phone_number, token)
            return f"✓ Synced {count} new transactions from TrueLayer."
        except Exception as e:
            logger.error(f"[finance_sync_transactions] Error: {e}")
            return f"Error syncing transactions: {e}"
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
        if name == "all":
            phone_number = arguments.get("phone_number")
            token = await _get_valid_token(phone_number)
            # Get accounts
            accounts = await _truelayer_get("data/v1/accounts", token)
            if not accounts["results"]:
                return "No accounts found."
            account_id = accounts["results"][0]["account_id"]
            txns = await _truelayer_get(f"data/v1/accounts/{account_id}/transactions", token)
            return json.dumps(txns["results"])
        elif name == "transactions_recent":
            phone_number = arguments.get("phone_number")
            logger.info(f"[get_recent_transactions] Called for phone_number={phone_number}")
            try:
                token = await _get_valid_token(phone_number)
                logger.info(f"[get_recent_transactions] Valid token obtained for user {phone_number}: {token[:8]}...")
            except Exception as e:
                logger.error(f"[get_recent_transactions] Error getting token for {phone_number}: {e}")
                return f"Error getting token: {e}"
            try:
                accounts = await _truelayer_get("data/v1/accounts", token)
                logger.info(f"[get_recent_transactions] Accounts response: {accounts}")
            except Exception as e:
                logger.error(f"[get_recent_transactions] Error fetching accounts: {e}")
                return f"Error fetching accounts: {e}"
            if not accounts.get("results"):
                logger.warning(f"[get_recent_transactions] No accounts found for user {phone_number}")
                return "No accounts found."
            account_id = accounts["results"][0]["account_id"]
            now = datetime.now(timezone.utc)
            since = (now - timedelta(hours=6)).isoformat()
            logger.info(f"[get_recent_transactions] Fetching transactions since {since} for account {account_id}")
            try:
                txns = await _truelayer_get(f"data/v1/accounts/{account_id}/transactions", token, params={"from": since})
                logger.info(f"[get_recent_transactions] Transactions response: {txns}")
            except Exception as e:
                logger.error(f"[get_recent_transactions] Error fetching transactions: {e}")
                return f"Error fetching transactions: {e}"
            txn_count = len(txns.get("results", []))
            logger.info(f"[get_recent_transactions] Returning {txn_count} transactions for user {phone_number}")
            return json.dumps(txns["results"])
        elif name == "balance":
            phone_number = arguments.get("phone_number")
            token = await _get_valid_token(phone_number)
            accounts = await _truelayer_get("data/v1/accounts", token)
            if not accounts["results"]:
                return "No accounts found."
            account_id = accounts["results"][0]["account_id"]
            balance = await _truelayer_get(f"data/v1/accounts/{account_id}/balance", token)
            return json.dumps(balance["results"][0])
        elif name == "category":
            phone_number = arguments.get("phone_number")
            category = arguments.get("category")
            token = await _get_valid_token(phone_number)
            accounts = await _truelayer_get("data/v1/accounts", token)
            if not accounts["results"]:
                return "No accounts found."
            account_id = accounts["results"][0]["account_id"]
            txns = await _truelayer_get(f"data/v1/accounts/{account_id}/transactions", token)
            filtered = [t for t in txns["results"] if t.get("transaction_category") == category]
            return json.dumps(filtered)
        elif name == "merchant":
            phone_number = arguments.get("phone_number")
            merchant = arguments.get("merchant")
            token = await _get_valid_token(phone_number)
            accounts = await _truelayer_get("data/v1/accounts", token)
            if not accounts["results"]:
                return "No accounts found."
            account_id = accounts["results"][0]["account_id"]
            txns = await _truelayer_get(f"data/v1/accounts/{account_id}/transactions", token)
            filtered = [t for t in txns["results"] if merchant.lower() in t.get("description", "").lower()]
            return json.dumps(filtered)
        elif name == "daterange":
            phone_number = arguments.get("phone_number")
            start_date = arguments.get("start_date")
            end_date = arguments.get("end_date")
            token = await _get_valid_token(phone_number)
            accounts = await _truelayer_get("data/v1/accounts", token)
            if not accounts["results"]:
                return "No accounts found."
            account_id = accounts["results"][0]["account_id"]
            txns = await _truelayer_get(f"data/v1/accounts/{account_id}/transactions", token, params={"from": start_date, "to": end_date})
            return json.dumps(txns["results"])
        elif name == "scheduledpayments":
            phone_number = arguments.get("phone_number")
            token = await _get_valid_token(phone_number)
            accounts = await _truelayer_get("data/v1/accounts", token)
            if not accounts["results"]:
                return "No accounts found."
            account_id = accounts["results"][0]["account_id"]
            payments = await _truelayer_get(f"data/v1/accounts/{account_id}/scheduled_payments", token)
            return json.dumps(payments["results"])
        elif name == "summary":
            phone_number = arguments.get("phone_number")
            period = arguments.get("period")
            token = await _get_valid_token(phone_number)
            accounts = await _truelayer_get("data/v1/accounts", token)
            if not accounts["results"]:
                return "No accounts found."
            account_id = accounts["results"][0]["account_id"]
            txns = await _truelayer_get(f"data/v1/accounts/{account_id}/transactions", token)
            now = datetime.now(timezone.utc)
            if period == "monthly":
                start = now.replace(day=1).date().isoformat()
            elif period == "weekly":
                start = (now - timedelta(days=now.weekday())).date().isoformat()
            else:
                return "Invalid period."
            filtered = [t for t in txns["results"] if t["timestamp"][:10] >= start]
            total = sum(float(t["amount"]['value']) for t in filtered if t["transaction_type"] == "debit")
            return json.dumps({"period": period, "start": start, "spending": total, "count": len(filtered)})
        elif name == "status":
            phone_number = arguments.get("phone_number")
            # For demo, just return current month spending and balance
            token = await _get_valid_token(phone_number)
            accounts = await _truelayer_get("data/v1/accounts", token)
            if not accounts["results"]:
                return "No accounts found."
            account_id = accounts["results"][0]["account_id"]
            txns = await _truelayer_get(f"data/v1/accounts/{account_id}/transactions", token)
            start = datetime.now(timezone.utc).replace(day=1).date().isoformat()
            filtered = [t for t in txns["results"] if t["timestamp"][:10] >= start]
            spent = sum(float(t["amount"]['value']) for t in filtered if t["transaction_type"] == "debit")
            balance = await _truelayer_get(f"data/v1/accounts/{account_id}/balance", token)
            bal = float(balance["results"][0]["current"])
            return json.dumps({"month_spent": spent, "current_balance": bal})
        elif name == "income":
            phone_number = arguments.get("phone_number")
            token = await _get_valid_token(phone_number)
            accounts = await _truelayer_get("data/v1/accounts", token)
            if not accounts["results"]:
                return "No accounts found."
            account_id = accounts["results"][0]["account_id"]
            txns = await _truelayer_get(f"data/v1/accounts/{account_id}/transactions", token)
            credits = [t for t in txns["results"] if t["transaction_type"] == "credit"]
            return json.dumps(credits)

        elif name == "add_category":
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
            text = f"Unknown finance tool: {name}"

    return text


def _apply_tips(transactions: list[dict], phone_number: str, tips: list[dict]) -> None:
    """Apply user tips to transactions, setting category if pattern matches."""
    for txn in transactions:
        desc = txn["description"].lower()
        for tip in tips:
            if tip["pattern"].lower() in desc:
                txn["category"] = tip["category"]
                break
