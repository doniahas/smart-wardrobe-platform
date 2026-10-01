from fastapi import APIRouter, Depends, HTTPException, Request, status
from pymongo.errors import DuplicateKeyError

from app.auth import hash_password, verify_password, create_access_token, get_current_user
from app.core.config import RATE_LIMIT_AUTH
from app.core.database import get_db
from app.core.limiter import limiter
from app.models import UserRegister, UserLogin, UserPublic, Token

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
@limiter.limit(RATE_LIMIT_AUTH)
async def register(request: Request, body: UserRegister, db=Depends(get_db)):
    doc = {
        "username": body.username,
        "email": body.email,
        "hashed_password": hash_password(body.password),
    }
    try:
        await db.users.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username or email already registered",
        )
    return UserPublic(username=body.username, email=body.email)


@router.post("/login", response_model=Token)
@limiter.limit(RATE_LIMIT_AUTH)
async def login(request: Request, body: UserLogin, db=Depends(get_db)):
    user = await db.users.find_one({"username": body.username})
    if user is None or not verify_password(body.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token({"sub": user["username"]})
    return Token(access_token=token)


@router.get("/me", response_model=UserPublic)
async def me(current_user: dict = Depends(get_current_user)):
    return UserPublic(username=current_user["username"], email=current_user["email"])
