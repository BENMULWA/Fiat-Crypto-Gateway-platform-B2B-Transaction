from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from datetime import datetime
import uuid
import requests
import asyncio

from database import get_db
from services.safaricom_daraja import DarajaService
from routes.auth import get_current_user

router = APIRouter(prefix="/api/ramp", tags=["Ramp & Swaps"])
mam_laka = DarajaService()

# Safe MongoDB ObjectId converter
try:
    from bson import ObjectId
except ImportError:
    ObjectId = None

def safe_object_id(val):
    if ObjectId and isinstance(val, str) and len(val) == 24:
        try:
            return ObjectId(val)
        except:
            pass
    return val

class RampExecute(BaseModel):
    direction: str
    channel: str
    from_asset: str
    to_asset: str
    amount: float
    rate: float
    fee: float
    counterparty: str

@router.post("/execute", status_code=201)
async def execute_ramp(body: RampExecute, db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = safe_object_id(current_user["_id"])
    trade_id = f"TRADE_{uuid.uuid4().hex[:8].upper()}"
    
    receive = body.amount

    # 🧮 UNIVERSAL SPREAD PROFIT CALCULATOR (Handles all 15 assets)
    profit_amount = 0.0
    profit_currency = body.from_asset

    if body.direction == "swap":
        # Baseline true rates relative to USD (1.0)
        usd_base_rates = {
            "USDA": 1.0, "USDC": 1.0, "USDT": 1.0, "USD": 1.0, "IMP": 1.0,
            "KES": 130.50, "UGX": 3750.00, "TZS": 2580.00, "RWF": 1320.00,
            "BIF": 2850.00, "XAF": 605.00, "XOF": 605.00, "AIRT": 130.50,
            "BTC": 1 / 64000, "ETH": 1 / 3500
        }

        rate_from = usd_base_rates.get(body.from_asset, 1.0)
        rate_to = usd_base_rates.get(body.to_asset, 1.0)
        
        # 1. Calculate the True amount of to_asset the user SHOULD get without a spread
        true_to_amount = (body.amount / rate_from) * rate_to
        
        # 2. Calculate the Actual amount given based on the quoted body.rate
        actual_to_amount = body.amount * body.rate
        
        # 3. The difference is the platform's spread profit!
        profit_in_to_asset = true_to_amount - actual_to_amount
        
        if profit_in_to_asset > 0:
            profit_amount = profit_in_to_asset
            profit_currency = body.to_asset
        else:
            # Fallback: If no spread exists, capture a default 1% convenience fee in the from_asset
            profit_amount = body.amount * 0.01
            profit_currency = body.from_asset

    # ========================================================
    # 💰 LOG COMPANY SPREAD & CORPORATE REVENUE ACCUMULATION
    # ========================================================
    if profit_amount > 0:
        settlement_doc = {
            "_id": f"PNL_{uuid.uuid4().hex[:6].upper()}",
            "desc": f"{body.amount} {body.from_asset} → {receive} {body.to_asset} ({body.channel})",
            "profit_amount": round(profit_amount, 4),
            "profit_currency": profit_currency,
            "channel": body.channel,
            "timestamp": datetime.utcnow(),
            "status": "COMPLETED",
            "trade_id": trade_id
        }
        await db["settlement_logs"].insert_one(settlement_doc)
        print(f"💰 Mamlaka Captured Spread Profit via [{body.channel}]: +{round(profit_amount, 4)} {profit_currency}")

        # 🟢 CORPORATE REVENUE ACCUMULATION
        await db["company_revenue"].update_one(
            {"_id": "corporate_treasury"},
            {"$inc": {profit_currency: profit_amount}},
            upsert=True
        )
        print(f"🏦 Profit safely locked in Corporate Revenue Wallet.")

    # ========================================================
    # INTERNAL SWAP EXECUTION (User Ledger Transfer)
    # ========================================================
    if body.direction == "swap":
        receive_amount = round(body.amount * body.rate, 4)
        
        # Lock & Verify Balance
        wallet = await db["retail_wallets"].find_one({"userId": user_id})
        current_balance = float(wallet.get(body.from_asset, 0)) if wallet else 0
        
        if current_balance < body.amount:
            raise HTTPException(status_code=400, detail=f"Insufficient {body.from_asset} balance. You only have {current_balance}.")

        # Atomic Database Update
        await db["retail_wallets"].update_one(
            {"userId": user_id},
            {
                "$inc": {
                    body.from_asset: -body.amount,
                    body.to_asset: receive_amount
                }
            }
        )

        # Log User Receipt
        history_doc = {
            "_id": trade_id,
            "direction": "swap",
            "channel": body.channel or "Internal Ledger",
            "fromAsset": body.from_asset,
            "toAsset": body.to_asset,
            "fromAmount": body.amount,
            "toAmount": receive_amount,
            "status": "completed",
            "userId": user_id,
            "date": datetime.utcnow().strftime("%b %d, %Y"),
            "timeAgo": "Just now",
            "createdAt": datetime.utcnow() 
        }
        await db["ramp_entries"].insert_one(history_doc)
        
        return {
            "id": trade_id, 
            "status": "completed", 
            "message": "Swap executed instantly.",
            "receive": receive_amount
        }

    # ========================================================
    # ON-RAMP (DEPOSIT KES VIA STK PUSH)
    # ========================================================
    if body.direction == "on" and body.channel == "Mobile Money":
        try:
            token = mam_laka.get_access_token()
            initiate_url = f"{mam_laka.base_url}/api/v1/mobile/initiate"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            payload = {
                "impalaMerchantId": "meshex_sandbox",
                "displayName": "Mamlaka Deposit",
                "currency": "KES",
                "amount": int(body.amount),
                "payerPhone": body.counterparty, 
                "mobileMoneySP": "M-Pesa",
                "externalId": trade_id,
                "callbackUrl": "https://hemathermal-ha-dextrously.ngrok-free.dev/api/ramp/b2c/result"
            }
            requests.post(initiate_url, json=payload, headers=headers, timeout=15)
        except Exception as e:
            print(f"❌ STK Error: {str(e)}")

        doc = {
            "_id": trade_id, "direction": body.direction, "channel": body.channel,
            "fromAsset": body.from_asset, "toAsset": body.to_asset,
            "fromAmount": body.amount, "toAmount": receive,
            "status": "processing", "userId": user_id,
            "date": datetime.utcnow().strftime("%b %d, %Y"),
            "timeAgo": datetime.utcnow().strftime("%H:%M:%S"),
            "createdAt": datetime.utcnow()
        }
        await db["ramp_entries"].insert_one(doc)
        return {"id": trade_id, "status": "processing...", "message": "STK Push sent!"}

    # ========================================================
    # OFF-RAMP (WITHDRAW TO M-PESA VIA B2C)
    # ========================================================
    elif body.direction == "off" and body.channel == "Mobile Money":
        
        # 1. Verify User's Internal KES Balance
        user_wallet = await db["retail_wallets"].find_one({"userId": user_id})
        current_kes = float(user_wallet.get("KES", 0.0)) if user_wallet else 0.0
        
        if current_kes < body.amount:
            raise HTTPException(status_code=400, detail=f"Insufficient KES balance. You have {current_kes} KES.")

        # 2. Lock/Deduct Funds from Internal Wallet
        await db["retail_wallets"].update_one(
            {"userId": user_id},
            {"$inc": {"KES": -body.amount}}
        )
        
        try:
            token = mam_laka.get_access_token()
            payout_url = f"{mam_laka.base_url}/api/v1/mobile/transfer"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            payload = {
                "impalaMerchantId": "meshex_sandbox",
                "currency": "KES",
                "amount": int(receive),
                "recipientPhone": body.counterparty,
                "mobileMoneySP": "M-Pesa",
                "externalId": trade_id,
                "callbackUrl": "https://hemathermal-ha-dextrously.ngrok-free.dev/api/ramp/b2c/result"
            }
            requests.post(payout_url, json=payload, headers=headers, timeout=15)
        except Exception as e:
            print(f"B2C Error: {e}")
            await db["retail_wallets"].update_one({"userId": user_id}, {"$inc": {"KES": body.amount}})
            raise HTTPException(status_code=502, detail="Failed to connect to Mam-laka API. Funds refunded.")

        doc = {
            "_id": trade_id, "direction": body.direction, "channel": body.channel,
            "fromAsset": body.from_asset, "toAsset": body.to_asset,
            "fromAmount": body.amount, "toAmount": receive,
            "status": "processing", "userId": user_id,
            "date": datetime.utcnow().strftime("%b %d, %Y"),
            "timeAgo": datetime.utcnow().strftime("%H:%M:%S"),
            "createdAt": datetime.utcnow()
        }
        await db["ramp_entries"].insert_one(doc)
        return {"id": trade_id, "receive": receive, "status": "processing", "message": "Withdrawal sent to M-Pesa!"}

    return {"id": trade_id, "receive": receive, "status": "processing"}
# ========================================================
# 🟢 SECURE WEBHOOK RECEIVER (ROBUST VERSION)
# ========================================================
@router.post("/b2c/result") 
async def mamlaka_stk_callback(payload: dict, db=Depends(get_db)):
    # 1. LOG RAW PAYLOAD (Crucial for debugging African APIs)
    print(f"📦 [WEBHOOK INCOMING] Raw Payload: {payload}")
    
    # 2. CHECK MULTIPLE POSSIBLE KEYS FOR THE STATUS
    # Different African gateways use different keys (status, transactionStatus, state)
    tx_status = str(payload.get("transactionStatus", "")).strip().upper()
    tx_state = str(payload.get("status", "")).strip().upper()
    tx_report = str(payload.get("transactionReport", "")).strip().upper()
    tx_message = str(payload.get("message", "")).strip().upper()

    # 3. SMART SUCCESS DETECTION
    # If ANY of the keys contain "SUCCESS" or the report says "PROCESSED SUCCESSFULLY", it's a win!
    is_success = (
        "SUCCESS" in tx_status or 
        "SUCCESS" in tx_state or 
        "COMPLETED" in tx_state or
        "PROCESSED SUCCESSFULLY" in tx_report or 
        "SUCCESS" in tx_message
    )

    external_id = payload.get("externalId", "") 
    
    if not external_id:
        return {"status": "ignored"}

    if external_id.startswith("TRADE_") or external_id.startswith("SWEEP_") or external_id.startswith("REV_"):
        trade = await db["ramp_entries"].find_one({"_id": external_id})
        
        if not trade:
            print(f"⚠️ [WEBHOOK] Trade {external_id} not found in database.")
            return {"status": "ignored"}

        # Prevent double-processing if M-Pesa sends the webhook twice
        if trade.get("status") not in ["processing", "pending"]:
            print(f"⏭️ [WEBHOOK] Trade {external_id} already processed ({trade.get('status')}). Ignoring duplicate.")
            return {"status": "already_processed"}
            
        # --- HANDLE SUCCESS ---
        if is_success:
            await db["ramp_entries"].update_one(
                {"_id": external_id}, 
                {"$set": {"status": "completed", "report": payload}}
            )
            
            if trade.get("direction") == "on":
                user_id = trade.get("userId")
                asset = trade.get("fromAsset", "KES")
                amount = float(trade.get("fromAmount", 0))
                
                await db["retail_wallets"].update_one(
                    {"userId": user_id},
                    {"$inc": {asset: amount}},
                    upsert=True
                )
                print(f"🟢 [WEBHOOK] SUCCESS! Credited {amount} {asset} to user {user_id}")
                
        # --- HANDLE FAILURE ---
        else:
            await db["ramp_entries"].update_one(
                {"_id": external_id}, 
                {"$set": {"status": "failed", "report": payload}}
            )
            print(f"🔴 [WEBHOOK] FAILED! Trade {external_id} rejected. Full payload saved to DB.")
            
            # If it was a withdrawal, give the money back
            if trade.get("direction") == "off":
                user_id = trade.get("userId")
                asset = trade.get("fromAsset", "KES")
                amount = float(trade.get("fromAmount", 0))
                await db["retail_wallets"].update_one(
                    {"userId": user_id}, 
                    {"$inc": {asset: amount}}
                )
                print(f"🔄 [WEBHOOK] Refunded {amount} {asset} back to user {user_id}")

    return {"status": "acknowledged"}

@router.get("/history")
async def get_ramp_history(db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = safe_object_id(current_user["_id"])
    cursor = db["ramp_entries"].find({"userId": user_id}).sort("createdAt", -1).limit(50)
    entries = await cursor.to_list(length=50)

    formatted_entries = []
    for e in entries:
        formatted_entries.append({
            "id": str(e["_id"]), 
            "direction": e.get("direction", "on"),
            "channel": e.get("channel", "Mobile Money"), 
            "fromAsset": e.get("fromAsset", "KES"),
            "toAsset": e.get("toAsset", "USDA"), 
            "fromAmount": e.get("fromAmount", 0),
            "toAmount": e.get("toAmount", 0), 
            "status": e.get("status", "completed"),
            "date": e.get("date", "Today"), 
            "timeAgo": e.get("timeAgo", "Recently"),
            # Send proper ISO timestamp to the frontend
            "createdAt": e.get("createdAt", datetime.utcnow()).isoformat() + "Z" if e.get("createdAt") else None
        })
    return {"status": "success", "entries": formatted_entries}