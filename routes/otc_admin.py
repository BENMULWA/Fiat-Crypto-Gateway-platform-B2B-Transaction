from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta
from database import get_db

try:
    from bson import ObjectId
except ImportError:
    ObjectId = None

router = APIRouter(prefix="/api/admin", tags=["OTC Admin Dashboard"])

def safe_obj_id(val):
    if ObjectId and isinstance(val, str) and len(val) == 24:
        try: return ObjectId(val)
        except: pass
    return val

# 🟢 FIX: Added Pydantic model to correctly catch the JSON body sent by React
class TxStatusUpdate(BaseModel):
    status: str

@router.get("/operations-overview")
async def get_operations_overview(db=Depends(get_db)):
    # Placeholder for dashboard kpis
    return {"status": "success", "kpis": {}}

@router.get("/dealer/rfqs")
async def get_incoming_rfqs():
    import random
    mock_rfqs = [
        {"id": f"RFQ-{random.randint(1000, 9999)}", "clientName": "Acme Hedge Fund", "channel": "API Integration", "asset": "USDC", "side": "BUY", "size": 250000.00, "status": "pending", "timeAgo": "Just now"}
    ]
    return {"status": "success", "rfqs": mock_rfqs}

@router.get("/analytics/chart-data")
async def get_chart_analytics(days: int = 7, db=Depends(get_db)):
    start_date = datetime.utcnow() - timedelta(days=days)
    pipeline = [
        {"$match": {"createdAt": {"$gte": start_date}, "status": {"$in": ["completed", "COMPLETED", "Completed"]}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%b %d", "date": "$createdAt"}},
            "volume": {"$sum": "$fromAmount"},
            "revenue": {"$sum": {"$multiply": ["$fromAmount", 0.015]}} 
        }},
        {"$sort": {"_id": 1}}
    ]
    data = await db["ramp_entries"].aggregate(pipeline).to_list(length=days)
    chart_data = [{"date": d["_id"], "volume": round(float(d.get("volume", 0)), 2), "revenue": round(float(d.get("revenue", 0)), 2)} for d in data]
    return {"status": "success", "chartData": chart_data}

@router.get("/retail-transactions")
async def get_all_retail_transactions(userId: str = None, limit: int = 200, db=Depends(get_db)):
    """Fetches all retail transactions, smartly resolving ObjectIds vs Strings"""
    query = {}
    if userId:
        # 🟢 FIX: Search for both String AND ObjectId to guarantee we find the data!
        or_conditions = [{"userId": userId}]
        try:
            from bson import ObjectId
            if len(userId) == 24:
                or_conditions.append({"userId": ObjectId(userId)})
        except:
            pass
        query["$or"] = or_conditions

    cursor = db["ramp_entries"].find(query).sort("createdAt", -1).limit(limit)
    entries = await cursor.to_list(length=limit)
    
    formatted_entries = []
    
    for e in entries:
        user_id = e.get("userId")
        customer_name = "Unknown User"
        if user_id:
            user = await db["users"].find_one({"_id": safe_obj_id(user_id)})
            if user: customer_name = user.get("displayName") or user.get("name") or user.get("email", "Unknown")

        formatted_entries.append({
            "id": str(e["_id"]),
            "createdAt": e.get("createdAt", datetime.utcnow()).isoformat() + "Z" if e.get("createdAt") else None,
            "customerName": customer_name,
            "direction": e.get("direction", "swap"),
            "fromAmount": e.get("fromAmount", 0), "fromAsset": e.get("fromAsset", ""),
            "toAmount": e.get("toAmount", 0), "toAsset": e.get("toAsset", ""),
            "status": e.get("status", "pending")
        })
    return {"status": "success", "entries": formatted_entries}

# 🟢 FIX: Use the payload Pydantic model and handle tx_id formats safely
@router.patch("/retail-transactions/{tx_id}/status")
async def moderate_transaction(tx_id: str, payload: TxStatusUpdate, db=Depends(get_db)):
    query = {"_id": tx_id}
    try:
        from bson import ObjectId
        if len(tx_id) == 24:
            query = {"$or": [{"_id": tx_id}, {"_id": ObjectId(tx_id)}]}
    except:
        pass

    result = await db["ramp_entries"].update_one(query, {"$set": {"status": payload.status, "moderatedAt": datetime.utcnow()}})
    if result.modified_count == 0: 
        raise HTTPException(status_code=404, detail="Transaction not found")
    return {"status": "success"}

@router.get("/compliance/kyc")
async def get_kyc_queue(db=Depends(get_db)):
    pending_users = await db["users"].find({"kycStatus": {"$in": ["pending", "PENDING"]}}).sort("createdAt", -1).limit(20).to_list(20)
    
    queue = []
    for u in pending_users:
        kyc_details = u.get("kycDetails", {})
        exact_name = kyc_details.get("fullName") or u.get("fullName") or u.get("displayName") or u.get("name", "Unknown")
        exact_email = kyc_details.get("email") or u.get("email", "Unknown")
        
        submitted_at = u.get("kycSubmittedAt") or u.get("createdAt") or datetime.utcnow()
        if isinstance(submitted_at, str):
            try: submitted_at = datetime.fromisoformat(submitted_at.replace('Z', '+00:00'))
            except: submitted_at = datetime.utcnow()
            
        diff = datetime.utcnow().replace(tzinfo=None) - submitted_at.replace(tzinfo=None)
        hours = int(diff.total_seconds() / 3600)
        mins = int((diff.total_seconds() % 3600) / 60)
        
        if hours > 24: time_ago = f"Submitted {hours // 24} days ago"
        elif hours > 0: time_ago = f"Submitted {hours} hours ago"
        elif mins > 0: time_ago = f"Submitted {mins} mins ago"
        else: time_ago = "Submitted just now"
        
        real_timestamp = submitted_at.strftime("%b %d, %Y, %I:%M %p")
        
        queue.append({
            "id": str(u.get("_id")),
            "name": exact_name,
            "email": exact_email,
            "timeAgo": time_ago,
            "realTimestamp": real_timestamp,
            "riskLevel": "low", "kycLevel": "Tier 1",
            "docs": {
                "id": True if kyc_details.get("documentName") or u.get("documentName") else False, 
                "selfie": False, "address": False, "source": False
            }
        })
        
    return {
        "status": "success",
        "kpis": {"pendingKyc": len(queue), "amlFlags": 0, "pepMatches": 0, "sanctions": 0, "riskAlerts": 0},
        "queue": queue
    }

@router.post("/compliance/kyc/{id}/approve")
async def approve_kyc(id: str, db=Depends(get_db)):
    res = await db["users"].update_one({"_id": safe_obj_id(id)}, {"$set": {"kycStatus": "verified"}})
    if res.modified_count == 0: raise HTTPException(404, "User not found")
    return {"status": "approved"}

@router.post("/compliance/kyc/{id}/reject")
async def reject_kyc(id: str, db=Depends(get_db)):
    res = await db["users"].update_one({"_id": safe_obj_id(id)}, {"$set": {"kycStatus": "rejected"}})
    if res.modified_count == 0: raise HTTPException(404, "User not found")
    return {"status": "rejected"}

@router.get("/finance/customers")
async def get_customers_list(db=Depends(get_db)):
    users = await db["users"].find().sort("createdAt", -1).limit(100).to_list(100)
    customers = []
    for u in users:
        wallet = await db["retail_wallets"].find_one({"userId": u["_id"]})
        total_vol = sum(float(v) for k, v in (wallet or {}).items() if k not in ["_id", "userId"])
        
        kyc_details = u.get("kycDetails", {})
        exact_name = kyc_details.get("fullName") or u.get("fullName") or u.get("displayName") or u.get("name", "Unknown")
        
        created_at = u.get("createdAt")
        joined_at_iso = created_at.isoformat() + "Z" if isinstance(created_at, datetime) else str(created_at) if created_at else None
        
        kyc_date = u.get("kycSubmittedAt", created_at)
        kyc_date_iso = kyc_date.isoformat() + "Z" if isinstance(kyc_date, datetime) else str(kyc_date) if kyc_date else None
        
        customers.append({
            "id": str(u.get("_id")),
            "name": exact_name,
            "legalName": exact_name,
            "email": kyc_details.get("email") or u.get("email", "Unknown"),
            "phone": kyc_details.get("phone") or u.get("phone", "N/A"),
            "idNumber": kyc_details.get("idNumber") or u.get("idNumber", "N/A"),
            "documentName": kyc_details.get("documentName") or u.get("documentName", "None provided"),
            "kycSubmittedAt": kyc_date_iso,
            "joinedAt": joined_at_iso,
            "kyc": u.get("kycStatus", "unverified").lower(),
            "risk": "low" if total_vol < 100000 else "high",
            "volume": f"KES {total_vol:,.0f}",
            "status": u.get("accountStatus", u.get("status", "active")),
        })
    return {"status": "success", "customers": customers}

@router.post("/compliance/customers/{id}/freeze")
async def freeze_customer(id: str, db=Depends(get_db)):
    res = await db["users"].update_one({"_id": safe_obj_id(id)}, {"$set": {"accountStatus": "frozen", "status": "frozen"}})
    return {"status": "frozen"}

@router.post("/compliance/customers/{id}/unfreeze")
async def unfreeze_customer(id: str, db=Depends(get_db)):
    res = await db["users"].update_one({"_id": safe_obj_id(id)}, {"$set": {"accountStatus": "active", "status": "active"}})
    return {"status": "active"}