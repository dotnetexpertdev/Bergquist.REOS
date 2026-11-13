"""Todoist integration for creating review tasks."""

from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, Tuple, List

import httpx

from ..config import settings


# Todoist retired REST v2 ("This endpoint is deprecated"); API v1 is the supported one.
TODOIST_API_URL = "https://api.todoist.com/api/v1"


def project_id() -> str:
    """The configured project's ID.

    Accepts the ID itself or the slug from the project's web address
    (e.g. ``property-demo-6j277vMvXWm8RXgr`` -> ``6j277vMvXWm8RXgr``).
    """
    value = (settings.todoist_project_id or "").strip().rstrip("/")
    value = value.rsplit("/", 1)[-1]
    return value.rsplit("-", 1)[-1] if "-" in value else value


def is_todoist_enabled() -> bool:
    """Check if Todoist integration is enabled and configured."""
    return settings.is_todoist_configured()


async def get_project_info(project_id: str) -> Tuple[Optional[Dict], Optional[str]]:
    """Get project information to verify connection."""
    if not is_todoist_enabled():
        return None, "Todoist not configured"

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{TODOIST_API_URL}/projects/{project_id}",
                headers={
                    "Authorization": f"Bearer {settings.todoist_api_token}",
                },
            )
            if response.status_code == 404:
                return None, f"Project {project_id} not found"
            if response.status_code != 200:
                return None, f"Failed to get project: {response.text}"

            return response.json(), None
        except Exception as e:
            return None, f"Project fetch error: {str(e)}"


async def create_task(
    content: str,
    description: str,
    due_date: Optional[date] = None,
    priority: int = 4,  # 1 = urgent, 4 = normal
    labels: Optional[List[str]] = None,
    request_id: Optional[str] = None,
) -> Tuple[Optional[Dict], Optional[str]]:
    """
    Create a task in Todoist.
    Returns (task, error)
    """
    if not is_todoist_enabled():
        return None, "Todoist not configured"

    # Calculate due date if not provided
    if not due_date:
        due_date = datetime.utcnow().date() + timedelta(days=settings.todoist_default_review_days)

    task_data = {
        "content": content,
        "description": description,
        "project_id": project_id(),
        "priority": priority,
        "due_date": due_date.isoformat(),
    }

    if labels:
        task_data["labels"] = labels

    headers = {
        "Authorization": f"Bearer {settings.todoist_api_token}",
        "Content-Type": "application/json",
    }

    if request_id:
        headers["X-Request-Id"] = request_id

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{TODOIST_API_URL}/tasks",
                headers=headers,
                json=task_data,
            )
            if response.status_code not in (200, 201):
                return None, f"Failed to create task: {response.text}"

            return response.json(), None

        except Exception as e:
            return None, f"Task creation error: {str(e)}"


async def update_task(
    task_id: str,
    content: Optional[str] = None,
    description: Optional[str] = None,
    due_date: Optional[date] = None,
    priority: Optional[int] = None,
    labels: Optional[List[str]] = None,
    request_id: Optional[str] = None,
) -> Tuple[Optional[Dict], Optional[str]]:
    """
    Update an existing task.
    Returns (task, error)
    """
    if not is_todoist_enabled():
        return None, "Todoist not configured"

    task_data = {}
    if content is not None:
        task_data["content"] = content
    if description is not None:
        task_data["description"] = description
    if due_date is not None:
        task_data["due_date"] = due_date.isoformat()
    if priority is not None:
        task_data["priority"] = priority
    if labels is not None:
        task_data["labels"] = labels

    if not task_data:
        return None, "No fields to update"

    headers = {
        "Authorization": f"Bearer {settings.todoist_api_token}",
        "Content-Type": "application/json",
    }

    if request_id:
        headers["X-Request-Id"] = request_id

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{TODOIST_API_URL}/tasks/{task_id}",
                headers=headers,
                json=task_data,
            )
            if response.status_code == 404:
                return None, f"Task {task_id} not found"
            if response.status_code not in (200, 204):
                return None, f"Failed to update task: {response.text}"

            # POST returns the updated task
            if response.status_code == 200:
                return response.json(), None
            return {"id": task_id}, None

        except Exception as e:
            return None, f"Task update error: {str(e)}"


async def get_task(task_id: str) -> Tuple[Optional[Dict], Optional[str]]:
    """Get a task by ID."""
    if not is_todoist_enabled():
        return None, "Todoist not configured"

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{TODOIST_API_URL}/tasks/{task_id}",
                headers={
                    "Authorization": f"Bearer {settings.todoist_api_token}",
                },
            )
            if response.status_code == 404:
                return None, f"Task {task_id} not found"
            if response.status_code != 200:
                return None, f"Failed to get task: {response.text}"

            return response.json(), None
        except Exception as e:
            return None, f"Task fetch error: {str(e)}"


async def find_task_by_text(text: str) -> Tuple[Optional[Dict], Optional[str]]:
    """An open task in the configured project whose description contains ``text``.

    Used before creating a task, so a task created by an earlier attempt whose reply
    was lost (network drop, crash) is adopted instead of duplicated.
    """
    if not is_todoist_enabled():
        return None, "Todoist not configured"
    async with httpx.AsyncClient() as client:
        try:
            cursor = None
            while True:  # API v1 pages its results: {"results": [...], "next_cursor": ...}
                params = {"project_id": project_id(), "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                response = await client.get(
                    f"{TODOIST_API_URL}/tasks", params=params,
                    headers={"Authorization": f"Bearer {settings.todoist_api_token}"},
                )
                if response.status_code != 200:
                    return None, f"Failed to list tasks: {response.text}"
                body = response.json() or {}
                tasks = body if isinstance(body, list) else body.get("results") or []
                for task in tasks:
                    if text in (task.get("description") or ""):
                        return task, None
                cursor = None if isinstance(body, list) else body.get("next_cursor")
                if not cursor:
                    return None, None
        except Exception as e:
            return None, f"Task lookup error: {str(e)}"


async def close_task(task_id: str) -> Tuple[bool, Optional[str]]:
    """Mark a task as complete."""
    if not is_todoist_enabled():
        return False, "Todoist not configured"

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{TODOIST_API_URL}/tasks/{task_id}/close",
                headers={
                    "Authorization": f"Bearer {settings.todoist_api_token}",
                },
            )
            if response.status_code == 404:
                return False, f"Task {task_id} not found"
            if response.status_code not in (200, 204):
                return False, f"Failed to close task: {response.text}"

            return True, None
        except Exception as e:
            return False, f"Task close error: {str(e)}"


def build_task_content(vendor_name: str, reason: str) -> str:
    """Build task title/content."""
    return f"Review bill from {vendor_name}: {reason}"


def build_task_description(
    document_url: str,
    vendor_name: Optional[str] = None,
    invoice_number: Optional[str] = None,
    total: Optional[str] = None,
    due_date: Optional[date] = None,
    qb_bill_id: Optional[str] = None,
) -> str:
    """Build task description with document details."""
    lines = [f"**Document:** {document_url}"]

    if vendor_name:
        lines.append(f"**Vendor:** {vendor_name}")
    if invoice_number:
        lines.append(f"**Invoice:** {invoice_number}")
    if total:
        lines.append(f"**Amount:** {total}")
    if due_date:
        lines.append(f"**Due:** {due_date.isoformat()}")
    if qb_bill_id:
        lines.append(f"**QuickBooks Bill:** {qb_bill_id}")

    return "\n".join(lines)
