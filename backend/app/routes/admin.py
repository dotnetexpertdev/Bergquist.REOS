"""Demo reset: preview and run a clean-up of this app's data and the bills it created."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_auth, require_csrf
from ..database import get_session
from ..services import demo_reset

router = APIRouter(prefix="/api/admin", tags=["admin"])


class ResetDemoRequest(BaseModel):
    confirm: str
    delete_quickbooks: bool = True


@router.get("/reset-demo")
async def reset_demo_preview(username: str = Depends(require_auth), db: AsyncSession = Depends(get_session)):
    """What a reset would delete. Changes nothing."""
    return await demo_reset.preview(db)


@router.post("/reset-demo")
async def reset_demo(request: ResetDemoRequest, username: str = Depends(require_auth),
                     db: AsyncSession = Depends(get_session), _csrf: None = Depends(require_csrf)):
    """Delete all documents, files and history, and the QuickBooks bills this app created."""
    if request.confirm.strip() != demo_reset.CONFIRM_WORD:
        raise HTTPException(status_code=400, detail=f"Type {demo_reset.CONFIRM_WORD} to confirm. Nothing was deleted.")
    try:
        return await demo_reset.reset(db, request.delete_quickbooks)
    except demo_reset.ResetError as e:
        await db.rollback()
        raise HTTPException(status_code=e.status_code, detail=e.message)
