#!/usr/bin/env python3
"""
Email tool handlers — Gmail integration for the personal agent.

Tools:
  email_list_recent  – List recent emails (subject, from, date, snippet)
  email_search       – Search emails by query
  email_read         – Read full email content by ID
  email_unread_count – Count unread emails
"""
import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import GmailUser

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
# Gmail Token Refresh
# ---------------------------------------------------------------------------

async def _get_valid_gmail_token(phone_number: str) -> str:
    """Return a valid Gmail access token, refreshing if expired."""
    async with _Session() as db:
        result = await db.execute(
            select(GmailUser).where(GmailUser.phone_number == phone_number)
        )
        user = result.scalar_one_or_none()
        if not user:
            raise Exception("Gmail not connected. Please connect your Gmail account first.")
        
        now = datetime.now(timezone.utc)
        if user.expires_at > now:
            return user.access_token
        
        # Refresh token
        GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
        GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
        
        url = "https://oauth2.googleapis.com/token"
        data = {
            "grant_type": "refresh_token",
            "refresh_token": user.refresh_token,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
        }
        
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, data=data)
            resp.raise_for_status()
            result = resp.json()
        
        user.access_token = result["access_token"]
        if "refresh_token" in result:
            user.refresh_token = result["refresh_token"]
        user.expires_at = datetime.now(timezone.utc) + timedelta(seconds=result.get("expires_in", 3600))
        await db.commit()
        
        return user.access_token


# ---------------------------------------------------------------------------
# Gmail API Helpers
# ---------------------------------------------------------------------------

async def _gmail_api_get(endpoint: str, access_token: str, params: dict = None) -> dict:
    """Make a GET request to Gmail API."""
    url = f"https://www.googleapis.com/gmail/v1/users/me/{endpoint}"
    headers = {"Authorization": f"Bearer {access_token}"}
    
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()


def _decode_email_body(payload: dict) -> str:
    """Extract and decode email body from Gmail API response."""
    body = ""
    
    if "body" in payload and payload["body"].get("data"):
        # Simple email with body directly in payload
        body = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")
    elif "parts" in payload:
        # Multipart email - look for text/plain or text/html
        for part in payload["parts"]:
            mime_type = part.get("mimeType", "")
            if mime_type == "text/plain" and part.get("body", {}).get("data"):
                body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
                break
            elif mime_type == "text/html" and part.get("body", {}).get("data") and not body:
                # Fall back to HTML if no plain text
                html = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
                # Basic HTML stripping
                import re
                body = re.sub(r'<[^>]+>', '', html)
            elif mime_type.startswith("multipart/"):
                # Nested multipart
                nested_body = _decode_email_body(part)
                if nested_body:
                    body = nested_body
                    break
    
    return body.strip()


def _get_header(headers: list, name: str) -> str:
    """Get a header value by name from Gmail headers list."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

async def call_tool(name: str, arguments: dict) -> str:
    
    if name == "email_list_recent":
        phone_number = arguments.get("phone_number")
        max_results = arguments.get("max_results", 10)
        
        try:
            token = await _get_valid_gmail_token(phone_number)
        except Exception as e:
            return str(e)
        
        try:
            # List recent messages
            messages_resp = await _gmail_api_get(
                "messages",
                token,
                params={"maxResults": min(max_results, 20)}
            )
            
            messages = messages_resp.get("messages", [])
            if not messages:
                return "No emails found."
            
            results = []
            for msg in messages:
                # Get message metadata
                msg_data = await _gmail_api_get(
                    f"messages/{msg['id']}",
                    token,
                    params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]}
                )
                
                headers = msg_data.get("payload", {}).get("headers", [])
                results.append({
                    "id": msg["id"],
                    "from": _get_header(headers, "From"),
                    "subject": _get_header(headers, "Subject"),
                    "date": _get_header(headers, "Date"),
                    "snippet": msg_data.get("snippet", ""),
                    "unread": "UNREAD" in msg_data.get("labelIds", []),
                })
            
            return json.dumps(results)
        
        except Exception as e:
            logger.error(f"[email_list_recent] Error: {e}")
            return f"Error fetching emails: {e}"
    
    elif name == "email_search":
        phone_number = arguments.get("phone_number")
        query = arguments.get("query", "")
        max_results = arguments.get("max_results", 10)
        
        if not query:
            return "✗ Search query is required."
        
        try:
            token = await _get_valid_gmail_token(phone_number)
        except Exception as e:
            return str(e)
        
        try:
            # Search messages
            messages_resp = await _gmail_api_get(
                "messages",
                token,
                params={"q": query, "maxResults": min(max_results, 20)}
            )
            
            messages = messages_resp.get("messages", [])
            if not messages:
                return f"No emails found matching '{query}'."
            
            results = []
            for msg in messages:
                msg_data = await _gmail_api_get(
                    f"messages/{msg['id']}",
                    token,
                    params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]}
                )
                
                headers = msg_data.get("payload", {}).get("headers", [])
                results.append({
                    "id": msg["id"],
                    "from": _get_header(headers, "From"),
                    "subject": _get_header(headers, "Subject"),
                    "date": _get_header(headers, "Date"),
                    "snippet": msg_data.get("snippet", ""),
                })
            
            return json.dumps({
                "query": query,
                "count": len(results),
                "results": results
            })
        
        except Exception as e:
            logger.error(f"[email_search] Error: {e}")
            return f"Error searching emails: {e}"
    
    elif name == "email_read":
        phone_number = arguments.get("phone_number")
        message_id = arguments.get("message_id", "")
        
        if not message_id:
            return "✗ Message ID is required."
        
        try:
            token = await _get_valid_gmail_token(phone_number)
        except Exception as e:
            return str(e)
        
        try:
            # Get full message
            msg_data = await _gmail_api_get(
                f"messages/{message_id}",
                token,
                params={"format": "full"}
            )
            
            headers = msg_data.get("payload", {}).get("headers", [])
            body = _decode_email_body(msg_data.get("payload", {}))
            
            return json.dumps({
                "id": message_id,
                "from": _get_header(headers, "From"),
                "to": _get_header(headers, "To"),
                "subject": _get_header(headers, "Subject"),
                "date": _get_header(headers, "Date"),
                "body": body[:5000] if body else "(no body content)",  # Limit body size
            })
        
        except Exception as e:
            logger.error(f"[email_read] Error: {e}")
            return f"Error reading email: {e}"
    
    elif name == "email_unread_count":
        phone_number = arguments.get("phone_number")
        
        try:
            token = await _get_valid_gmail_token(phone_number)
        except Exception as e:
            return str(e)
        
        try:
            # Get unread count from INBOX label
            label_data = await _gmail_api_get("labels/INBOX", token)
            unread = label_data.get("messagesUnread", 0)
            total = label_data.get("messagesTotal", 0)
            
            return json.dumps({
                "unread": unread,
                "total": total,
                "message": f"You have {unread} unread email(s) in your inbox."
            })
        
        except Exception as e:
            logger.error(f"[email_unread_count] Error: {e}")
            return f"Error getting unread count: {e}"
    
    return f"Unknown email tool: {name}"
