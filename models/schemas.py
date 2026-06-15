from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime


class SignupRequest(BaseModel):
    displayName: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=6)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    displayName: str
    role: str
    workspaceId: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class QuoteCreate(BaseModel):
    pair: str
    bankSpread: float = Field(..., gt=0)
    bankRef: str


class OrderCreate(BaseModel):
    side: str  # 'buy' | 'sell'
    price: float = Field(..., gt=0)
    amount: float = Field(..., gt=0)


class RampExecute(BaseModel):
    direction: str
    channel: str
    from_asset: str = Field(alias="from")
    to_asset: str = Field(alias="to")
    amount: float = Field(..., gt=0)
    rate: float = 1.0
    fee: float = 0.0
    counterparty: Optional[str] = None

    class Config:
        populate_by_name = True


class MintRequest(BaseModel):
    amount: float = Field(..., gt=0)
    network: str
    country: str
    note: Optional[str] = None


class RedeemRequest(BaseModel):
    amount: float = Field(..., gt=0)
    network: str
    country: str
    note: Optional[str] = None


class DiscountRateCreate(BaseModel):
    network: str
    country: str
    product: str
    rate: float = Field(..., gt=0, le=100)


# Cardano 
class CardanoVerifyDepositRequest(BaseModel):
    tx_hash: str = Field(..., min_length=64, max_length=64, description="64-char Cardano tx hash")
    counterparty: Optional[str] = None


class CardanoWithdrawRequest(BaseModel):
    to_address: str = Field(..., min_length=50, description="Destination bech32 Cardano address")
    amount: float = Field(..., gt=0, description="USDA amount (e.g. 10.5)")
    counterparty: Optional[str] = None