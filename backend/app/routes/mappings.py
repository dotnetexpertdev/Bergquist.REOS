"""Company-scoped vendor and expense-account mappings."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_auth, require_csrf
from ..database import get_session
from ..models import AccountMapping, VendorMapping
from ..schemas import (
    AccountMappingCreateRequest, AccountMappingResponse, VendorMappingCreateRequest, VendorMappingResponse,
)
from ..services.extraction import connected_realm

router = APIRouter(prefix="/api/mappings", tags=["mappings"])


async def _realm(db: AsyncSession) -> str:
    realm = await connected_realm(db)
    if not realm:
        raise HTTPException(status_code=400, detail="Connect QuickBooks first; mappings belong to one company.")
    return realm


@router.get("/vendors", response_model=List[VendorMappingResponse])
async def list_vendor_mappings(username: str = Depends(require_auth), db: AsyncSession = Depends(get_session)):
    realm = await connected_realm(db)
    rows = (await db.execute(select(VendorMapping).where(VendorMapping.realm_id == realm)
                             .order_by(VendorMapping.extracted_name))).scalars().all()
    return [VendorMappingResponse.model_validate(m) for m in rows]


@router.post("/vendors", response_model=VendorMappingResponse)
async def create_vendor_mapping(request: VendorMappingCreateRequest, username: str = Depends(require_auth),
                                db: AsyncSession = Depends(get_session), _csrf: None = Depends(require_csrf)):
    realm = await _realm(db)
    name = request.extracted_name.strip()
    exists = (await db.execute(select(VendorMapping).where(
        VendorMapping.realm_id == realm, func.lower(VendorMapping.extracted_name) == name.lower()))).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail="A mapping for this vendor name already exists")
    mapping = VendorMapping(extracted_name=name, qb_vendor_id=request.qb_vendor_id,
                            qb_vendor_name=request.qb_vendor_name, realm_id=realm)
    db.add(mapping)
    await db.flush()
    return VendorMappingResponse.model_validate(mapping)


@router.delete("/vendors/{mapping_id}")
async def delete_vendor_mapping(mapping_id: str, username: str = Depends(require_auth),
                                db: AsyncSession = Depends(get_session), _csrf: None = Depends(require_csrf)):
    mapping = await db.get(VendorMapping, mapping_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")
    await db.delete(mapping)
    return {"success": True}


@router.get("/accounts", response_model=List[AccountMappingResponse])
async def list_account_mappings(username: str = Depends(require_auth), db: AsyncSession = Depends(get_session)):
    realm = await connected_realm(db)
    rows = (await db.execute(select(AccountMapping).where(AccountMapping.realm_id == realm)
                             .order_by(AccountMapping.category))).scalars().all()
    return [AccountMappingResponse.model_validate(m) for m in rows]


@router.post("/accounts", response_model=AccountMappingResponse)
async def create_account_mapping(request: AccountMappingCreateRequest, username: str = Depends(require_auth),
                                 db: AsyncSession = Depends(get_session), _csrf: None = Depends(require_csrf)):
    realm = await _realm(db)
    category = request.category.strip().lower()
    exists = (await db.execute(select(AccountMapping).where(
        AccountMapping.realm_id == realm, AccountMapping.category == category))).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail="A mapping for this keyword already exists")
    mapping = AccountMapping(category=category, qb_account_id=request.qb_account_id,
                             qb_account_name=request.qb_account_name, realm_id=realm)
    db.add(mapping)
    await db.flush()
    return AccountMappingResponse.model_validate(mapping)


@router.delete("/accounts/{mapping_id}")
async def delete_account_mapping(mapping_id: str, username: str = Depends(require_auth),
                                 db: AsyncSession = Depends(get_session), _csrf: None = Depends(require_csrf)):
    mapping = await db.get(AccountMapping, mapping_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")
    await db.delete(mapping)
    return {"success": True}
