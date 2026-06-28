from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health_check():
    return {"status": "ok", "service": "AI Trading Bot"}


@router.get("/")
def root():
    return {"message": "AI Trading Bot API"}
