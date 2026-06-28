from fastapi import APIRouter, HTTPException

from app.database import asset_store
from app.schemas import Asset, AssetCreate, AssetUpdate

router = APIRouter()


@router.get("/assets/", response_model=dict)
def list_assets():
    return {"items": asset_store.list_assets()}


@router.post("/assets/", response_model=Asset)
def create_asset(asset_in: AssetCreate):
    return asset_store.create_asset(asset_in)


@router.get("/assets/{symbol}", response_model=Asset)
def get_asset(symbol: str):
    asset = asset_store.get_asset(symbol)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


@router.put("/assets/{symbol}", response_model=Asset)
def update_asset(symbol: str, asset_in: AssetUpdate):
    asset = asset_store.update_asset(symbol, asset_in)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


@router.delete("/assets/{symbol}", response_model=Asset)
def delete_asset(symbol: str):
    asset = asset_store.delete_asset(symbol)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset
