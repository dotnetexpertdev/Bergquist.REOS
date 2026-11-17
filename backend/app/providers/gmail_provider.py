"""Gmail integration for importing emails with attachments."""

import base64
import email
from urllib.parse import urlencode
from email import policy
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
import tempfile

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import ProviderConnection
from ..encryption import encrypt_token, decrypt_token


GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]


def get_gmail_auth_url(state: str) -> str:
    """Generate the Gmail OAuth authorization URL."""
    params = {
        "client_id": settings.gmail_client_id,
        "redirect_uri": settings.gmail_redirect_uri,
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"


async def exchange_gmail_code(code: str) -> Tuple[Optional[Dict], Optional[str]]:
    """Exchange authorization code for tokens."""
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.gmail_client_id,
                    "client_secret": settings.gmail_client_secret,
                    "redirect_uri": settings.gmail_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            if response.status_code != 200:
                    return None, f"Token exchange failed (HTTP {response.status_code})"

            tokens = response.json()
            return tokens, None
        except Exception as e:
            return None, f"Token exchange error: {type(e).__name__}"


async def refresh_gmail_token(refresh_token: str) -> Tuple[Optional[Dict], Optional[str]]:
    """Refresh an expired access token."""
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "refresh_token": refresh_token,
                    "client_id": settings.gmail_client_id,
                    "client_secret": settings.gmail_client_secret,
                    "grant_type": "refresh_token",
                },
            )
            if response.status_code != 200:
                return None, f"Token refresh failed: {response.text}"

            tokens = response.json()
            return tokens, None
        except Exception as e:
            return None, f"Token refresh error: {str(e)}"


async def get_gmail_tokens(db: AsyncSession) -> Tuple[Optional[str], Optional[str]]:
    """Get valid Gmail tokens, refreshing if needed."""
    result = await db.execute(
        select(ProviderConnection).where(ProviderConnection.provider == "gmail")
    )
    conn = result.scalar_one_or_none()

    if not conn or not conn.is_valid:
        return None, "Gmail not connected"

    access_token = decrypt_token(conn.access_token_encrypted) if conn.access_token_encrypted else None
    refresh_token = decrypt_token(conn.refresh_token_encrypted) if conn.refresh_token_encrypted else None

    if not access_token or not refresh_token:
        return None, "Gmail tokens not available"

    # Check if token needs refresh
    if conn.token_expiry and datetime.utcnow() >= conn.token_expiry:
        tokens, error = await refresh_gmail_token(refresh_token)
        if error:
            conn.is_valid = False
            conn.last_error = error
            await db.commit()
            return None, error

        # Update tokens
        conn.access_token_encrypted = encrypt_token(tokens["access_token"])
        conn.token_expiry = datetime.utcnow() + timedelta(seconds=tokens.get("expires_in", 3600))
        conn.last_refreshed_at = datetime.utcnow()
        access_token = tokens["access_token"]

        if "refresh_token" in tokens:
            conn.refresh_token_encrypted = encrypt_token(tokens["refresh_token"])
        await db.commit()  # persist refreshed credentials

    return access_token, None


from datetime import timedelta


async def get_label_id(access_token: str, label_name: str) -> Tuple[Optional[str], Optional[str]]:
    """Get the label ID for a label name."""
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/labels",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if response.status_code != 200:
                return None, f"Failed to fetch labels: {response.text}"

            labels = response.json().get("labels", [])
            for label in labels:
                if label["name"].lower() == label_name.lower():
                    return label["id"], None

            return None, (
                f"Gmail label '{label_name}' was not found. In Gmail, choose 'Create new label' "
                f"in the left sidebar, name it exactly '{label_name}', apply it to the vendor's "
                "email, then click Import From Inbox again."
            )
        except Exception as e:
            return None, f"Label lookup error: {str(e)}"


async def list_labeled_messages(
    access_token: str,
    label_id: str,
    max_results: int = 50,
) -> Tuple[List[Dict], Optional[str]]:
    """List messages with the specified label."""
    messages = []
    page_token = None

    async with httpx.AsyncClient() as client:
        while True:
            params = {
                "labelIds": label_id,
                "maxResults": min(max_results - len(messages), 100),
            }
            if page_token:
                params["pageToken"] = page_token

            try:
                response = await client.get(
                    "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                )
                if response.status_code != 200:
                    return [], f"Failed to list messages: {response.text}"

                data = response.json()
                messages.extend(data.get("messages", []))

                if len(messages) >= max_results or "nextPageToken" not in data:
                    break

                page_token = data["nextPageToken"]

            except Exception as e:
                return [], f"Message listing error: {str(e)}"

    return messages[:max_results], None


async def get_message_raw(access_token: str, message_id: str) -> Tuple[Optional[bytes], Optional[str]]:
    """Get raw message bytes."""
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"format": "raw"},
            )
            if response.status_code != 200:
                return None, f"Failed to get message: {response.text}"

            data = response.json()
            raw_bytes = base64.urlsafe_b64decode(data["raw"])
            return raw_bytes, None

        except Exception as e:
            return None, f"Message fetch error: {str(e)}"


async def get_message_metadata(access_token: str, message_id: str) -> Tuple[Optional[Dict], Optional[str]]:
    """Get message metadata including headers."""
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
            )
            if response.status_code != 200:
                return None, f"Failed to get message metadata: {response.text}"

            return response.json(), None

        except Exception as e:
            return None, f"Metadata fetch error: {str(e)}"


def parse_email_date(date_str: str) -> Optional[datetime]:
    """Parse email date header to datetime."""
    from email.utils import parsedate_to_datetime
    from datetime import timezone
    try:
        parsed = parsedate_to_datetime(date_str)
    except Exception:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)  # store UTC
    return parsed


class EmailAttachment:
    """Represents an email attachment."""

    def __init__(
        self,
        message_id: str,
        attachment_id: str,
        filename: str,
        content_type: str,
        size: int,
        data: Optional[bytes] = None,
    ):
        self.message_id = message_id
        self.attachment_id = attachment_id
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self.data = data


def extract_attachments(raw_email: bytes, message_id: str) -> List[EmailAttachment]:
    """Extract attachments from raw email bytes."""
    msg = email.message_from_bytes(raw_email, policy=policy.default)
    attachments = []

    def process_part(part, part_index: int):
        content_type = part.get_content_type()
        content_disposition = str(part.get("Content-Disposition", ""))

        # Check if this is an attachment
        if "attachment" in content_disposition or content_type in ("application/pdf", "image/jpeg", "image/png"):
            filename = part.get_filename() or f"attachment_{part_index}"
            payload = part.get_payload(decode=True)

            if payload:
                attachments.append(EmailAttachment(
                    message_id=message_id,
                    attachment_id=f"{message_id}_{part_index}",
                    filename=filename,
                    content_type=content_type,
                    size=len(payload),
                    data=payload,
                ))

    part_index = 0
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            process_part(part, part_index)
            part_index += 1
    else:
        process_part(msg, 0)

    return attachments


async def save_raw_email(raw_email: bytes, message_id: str) -> Path:
    """Save raw email to storage."""
    email_dir = settings.emails_path / message_id[:2]
    email_dir.mkdir(parents=True, exist_ok=True)

    email_path = email_dir / f"{message_id}.eml"
    with open(email_path, "wb") as f:
        f.write(raw_email)

    return email_path


async def save_attachment_temp(attachment: EmailAttachment) -> Path:
    """Save attachment to a temporary file."""
    suffix = Path(attachment.filename).suffix or ".bin"
    fd, temp_path = tempfile.mkstemp(suffix=suffix, dir=str(settings.temp_path))

    try:
        with open(fd, "wb") as f:
            f.write(attachment.data)
    except Exception:
        import os
        os.close(fd)
        raise

    return Path(temp_path)


def parse_headers(raw_email: bytes) -> Dict[str, Any]:
    """Sender, subject and date taken from the preserved raw message."""
    msg = email.message_from_bytes(raw_email, policy=policy.default)
    return {
        "from": str(msg.get("From", "") or ""),
        "subject": str(msg.get("Subject", "") or ""),
        "date": parse_email_date(str(msg.get("Date", "") or "")),
    }


class GmailAPI:
    """Read-only Gmail client bound to one access token."""

    def __init__(self, access_token: str):
        self.access_token = access_token

    async def get_label_id(self, label_name: str):
        return await get_label_id(self.access_token, label_name)

    async def list_labeled_messages(self, label_id: str):
        return await list_labeled_messages(self.access_token, label_id)

    async def get_message_raw(self, message_id: str):
        return await get_message_raw(self.access_token, message_id)
