"""Property management routes."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..auth import require_auth, require_csrf
from ..models import Property
from ..schemas import PropertyResponse, PropertyCreateRequest


router = APIRouter(prefix="/api/properties", tags=["properties"])


@router.get("", response_model=List[PropertyResponse])
async def list_properties(
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
):
    """List all properties."""
    result = await db.execute(
        select(Property).order_by(Property.name)
    )
    return [PropertyResponse.model_validate(p) for p in result.scalars().all()]


@router.post("", response_model=PropertyResponse)
async def create_property(
    request: PropertyCreateRequest,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    _csrf: None = Depends(require_csrf),
):
    """Create a new property."""
    prop = Property(
        name=request.name,
        address=request.address,
    )
    db.add(prop)
    await db.commit()
    await db.refresh(prop)
    return PropertyResponse.model_validate(prop)


@router.get("/{property_id}", response_model=PropertyResponse)
async def get_property(
    property_id: str,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
):
    """Get a property by ID."""
    result = await db.execute(
        select(Property).where(Property.id == property_id)
    )
    prop = result.scalar_one_or_none()
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found")
    return PropertyResponse.model_validate(prop)
