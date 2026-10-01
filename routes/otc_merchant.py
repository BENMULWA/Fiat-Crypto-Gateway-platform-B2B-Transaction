"""
Merchant-facing OTC endpoints -- the self-service counterpart to the
admin-initiated dealer desk in routes/otc_admin.py. Institutional customers
sign up (routes/auth.py::signup_verify_otp with account_type="institutional"),
complete onboarding here, fund their institutional_wallets balance, and
create/negotiate/accept their own RFQs -- which flow into the *same*
dealer_rfqs/dealer_settlements pipeline and treasury action state machine
already built for the admin-initiated path. Nothing about quoting,
screening, or settlement is duplicated here; only the entry point is new.
"""
import uuid
from settlement_networks import (
    SETTLEMENT_CHANNELS, NETWORKS, COUNTRIES, is_fiat, validate_wallet_address, network_supports_asset, public_options,
)
from datetime import datetime

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from database import get_db
from routes.auth import get_current_user, get_current_user_with_role, is_admin_role
from routes.otc_admin import (
    _fetch_dealer_rfq,
    _serialize_dealer_rfq,
    _accept_dealer_rfq_core,
    AnalysisEngine,
)
from routes.treasury import get_or_create_rate_book, compute_swap_quote_from_book
from broadcast import broadcast_manager
from notifications import notify_user
from institutional_wallet_utils import get_institutional_wallet

router = APIRouter(prefix="/api/otc", tags=["OTC Merchant Portal"])


# --- Institutional-approval gate -------------------------------------------
# Mirrors routes/auth.py::get_verified_current_user's pattern exactly (an
# opt-in stricter dependency layered on top of get_current_user, not a
# change to it) but checks institutional onboarding approval instead of
# retail KYC -- the two are different gates for different account types and
# must not be conflated onto the same kycStatus field.
async def get_verified_institutional_user(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    try:
        user_id = ObjectId(str(current_user.get("_id")))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid user identity.") from exc

    user_doc = await db.users.find_one({"_id": user_id}, {"role": 1, "businessName": 1})
    if not user_doc:
        raise HTTPException(status_code=401, detail="User session is no longer valid.")
    role = user_doc.get("role", "retail")
    if role not in ("institutional", "merchant"):
        raise HTTPException(status_code=403, detail="This account is not an institutional OTC account.")

    profile = await db["institutional_profiles"].find_one({"userId": str(user_id)})
    onboarding_status = str((profile or {}).get("onboardingStatus", "not_started"))
    if onboarding_status != "approved":
        raise HTTPException(
            status_code=403,
            detail=f"Onboarding must be approved before using OTC settlement (current status: {onboarding_status}).",
        )

    return {**current_user, "role": role, "businessName": user_doc.get("businessName"), "onboardingStatus": onboarding_status}


async def _get_own_profile(db, user_id: str) -> dict:
    profile = await db["institutional_profiles"].find_one({"userId": user_id})
    if not profile:
        raise HTTPException(status_code=404, detail="Institutional profile not found")
    return profile


# --- Onboarding --------------------------------------------------------------

@router.get("/onboarding")
async def get_onboarding_status(db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    profile = await db["institutional_profiles"].find_one({"userId": str(current_user["_id"])})
    if not profile:
        raise HTTPException(status_code=404, detail="Institutional profile not found -- sign up as an institutional account first")
    profile.pop("_id", None)

    # Real account-manager attribution: the admin who actually reviewed and
    # approved this merchant's KYB -- a genuine relationship that exists
    # from day one, not a fabricated "assigned dealer" persona and not
    # gated behind having placed a trade yet (unlike quote.quotedBy, which
    # only exists once an RFQ has been quoted).
    reviewed_by = profile.get("reviewedBy")
    if reviewed_by:
        try:
            reviewer = await db.users.find_one({"_id": ObjectId(str(reviewed_by))}, {"displayName": 1, "email": 1})
        except Exception:
            reviewer = None
        if reviewer:
            profile["reviewedByName"] = reviewer.get("displayName") or reviewer.get("email")

    return {"status": "success", "profile": profile}


@router.put("/onboarding/business-overview")
async def update_business_overview(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    user_id = str(current_user["_id"])
    await _get_own_profile(db, user_id)
    fields = {k: payload.get(k) for k in (
        "legalName", "companyType", "businessModel", "incorporationNumber",
        "dateOfIncorporation", "countryOfIncorporation", "taxNumber",
        "companyAddress", "zipCode", "state", "city", "businessDescription", "companyWebsite",
        "usePayIns", "usePayOuts", "useConversions",
        "linkedin", "facebook", "twitter", "instagram",
    ) if k in payload}
    fields["updatedAt"] = datetime.utcnow()
    await db["institutional_profiles"].update_one({"userId": user_id}, {"$set": fields})
    return {"status": "success"}


async def _update_onboarding_list(db, user_id: str, field: str, items: list):
    await _get_own_profile(db, user_id)
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail=f"{field} must be a list")
    await db["institutional_profiles"].update_one(
        {"userId": user_id},
        {"$set": {field: items, "updatedAt": datetime.utcnow()}},
    )
    return {"status": "success"}


@router.put("/onboarding/directors")
async def update_directors(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    return await _update_onboarding_list(db, str(current_user["_id"]), "directors", payload.get("directors", []))


@router.put("/onboarding/shareholders")
async def update_shareholders(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    return await _update_onboarding_list(db, str(current_user["_id"]), "shareholders", payload.get("shareholders", []))


@router.put("/onboarding/peps")
async def update_peps(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    return await _update_onboarding_list(db, str(current_user["_id"]), "peps", payload.get("peps", []))


@router.put("/onboarding/documents")
async def update_documents(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    """
    Records document metadata (name/type/reference) against the profile.
    Actual file storage should reuse whatever mechanism routes/retail.py's
    /kyc/submit already uses for retail KYC documents rather than a second
    upload pipeline -- wire that in here once confirmed, this endpoint just
    persists the resulting references.
    """
    return await _update_onboarding_list(db, str(current_user["_id"]), "documents", payload.get("documents", []))


@router.post("/onboarding/submit")
async def submit_onboarding(db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    user_id = str(current_user["_id"])
    profile = await _get_own_profile(db, user_id)
    missing = [f for f in ("directors", "shareholders", "documents") if not profile.get(f)]
    if not profile.get("legalName") and not profile.get("businessName"):
        missing.append("legalName")
    if missing:
        raise HTTPException(status_code=400, detail=f"Cannot submit onboarding -- missing: {', '.join(missing)}")

    now = datetime.utcnow()
    await db["institutional_profiles"].update_one(
        {"userId": user_id},
        {"$set": {"onboardingStatus": "under_review", "submittedAt": now, "updatedAt": now}},
    )
    await db["admin_notifications"].insert_one({
        "category": "institutional_onboarding",
        "type": "onboarding_submitted",
        "title": f"Institutional onboarding submitted: {profile.get('businessName', user_id)}",
        "message": "Review directors, shareholders, PEPs and documents, then approve or reject.",
        "route": "/admin/compliance",
        "isRead": False,
        "sourceUserId": user_id,
        "createdAt": now,
    })
    return {"status": "success", "onboardingStatus": "under_review"}


# --- Wallet (read-only from the merchant side; crediting is a treasury/admin action) ---

@router.get("/wallet")
async def get_my_wallet(db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    wallet = await get_institutional_wallet(db, str(current_user["_id"]))
    wallet.pop("_id", None)

    # Every account -- institutional included, issue_auth_payload doesn't
    # discriminate by role -- already gets a real derived Celo address at
    # signup. Retail's wallet page already surfaces it (routes/retail.py::
    # get_retail_wallet_balances); it was just never shown on the OTC side,
    # so merchants had no real deposit destination for crypto at all.
    # Best-effort: an RPC hiccup here must never break the wallet page.
    onchain = None
    try:
        from celo_wallet import get_onchain_celo_snapshot
        onchain = await get_onchain_celo_snapshot(db, current_user["_id"])
    except Exception as e:
        print(f"⚠️ Could not load on-chain snapshot for OTC wallet page: {e}")

    return {"status": "success", "balances": wallet, "onchain": onchain}


# --- Live rate ticker ---------------------------------------------------
# Same market sources the dealer desk quotes RFQs off of (see fx_feed) -- this is a read-only, unauthenticated-amount
# (amount=1) indicative snapshot for the dashboard ticker, not a quote a
# merchant can accept; that still only happens through POST /rfqs.
OTC_TICKER_PAIRS: list[tuple[str, str]] = [
    ("USDT", "KES"), ("USDC", "KES"), ("USDA", "KES"), ("USDC", "NGN"), ("BTC", "USDT"), ("USDT", "UGX"), ("USDT", "GHS"),
]


@router.get("/rates")
async def get_otc_rates(db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    """Indicative ticker. Same automatic source as the dealer's quotes (CBK for
    KES pairs, Live market otherwise). Never falls back to the desk's fixed rate
    book: if no current rate can be sourced for a pair, that pair is left out, and
    merchants see "unavailable" rather than a stale number. The final rate is
    always the dealer's quote."""
    from services.fx_feed import apply_cbk_rates, apply_live_rates, LiveRateUnavailable
    rate_book = await get_or_create_rate_book(db)
    rates = []
    as_of = None
    for from_asset, to_asset in OTC_TICKER_PAIRS:
        priced = None
        order = (apply_cbk_rates, apply_live_rates) if "KES" in (from_asset, to_asset) else (apply_live_rates,)
        for apply in order:
            try:
                priced = await apply(db, rate_book, [from_asset, to_asset])
                break
            except LiveRateUnavailable:
                continue
        if not priced:
            continue
        book, info = priced
        try:
            quote = compute_swap_quote_from_book(from_asset, to_asset, 1.0, book)
        except Exception:
            continue
        rates.append({
            "pair": f"{from_asset}/{to_asset}",
            "fromAsset": from_asset,
            "toAsset": to_asset,
            "rate": quote.get("execution_rate"),
        })
        fetched = info.get("marketFetchedAt")
        as_of = max(filter(None, [as_of, fetched]), default=as_of)
    return {
        "status": "success",
        "rates": rates,
        "active": bool(rate_book.get("active", True)),
        "unavailable": len(rates) == 0,
        "indicative": True,
        "updatedAt": as_of,
    }


# --- Merchant-initiated RFQs -------------------------------------------------

@router.get("/settlement-options")
async def get_settlement_options(current_user: dict = Depends(get_current_user)):
    return {"status": "success", **public_options()}


@router.post("/rfqs")
async def create_own_rfq(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_verified_institutional_user)):
    amount = float(payload.get("amount", 0) or 0)
    from_asset = str(payload.get("from_asset", "")).strip().upper()
    to_asset = str(payload.get("to_asset", "")).strip().upper()
    if amount <= 0 or not from_asset or not to_asset:
        raise HTTPException(status_code=400, detail="Valid amount, sell asset, and buy asset are required")

    # Where the proceeds go. Default is the merchant's own Jasiri wallet;
    # otherwise a bank account (treasury settles fiat to banks only, never
    # mobile money) or an external wallet on a named network.
    channel = str(payload.get("settlement_channel") or "WALLET_BALANCE").strip().upper()
    if channel not in SETTLEMENT_CHANNELS:
        raise HTTPException(status_code=400, detail="settlement_channel must be WALLET_BALANCE, TO_BANK or TO_EXTERNAL_WALLET")
    bank_details = None
    network = None
    destination_wallet = None
    if channel == "TO_BANK":
        if not is_fiat(to_asset):
            raise HTTPException(status_code=400, detail=f"{to_asset} is not a fiat currency; choose a fiat currency to settle to a bank account")
        raw = payload.get("bank_details") or {}
        bank_details = {k: str(raw.get(k) or "").strip() for k in ("country", "bankName", "accountNumber", "accountName", "swift")}
        missing = [k for k in ("country", "bankName", "accountNumber", "accountName") if not bank_details[k]]
        if missing:
            raise HTTPException(status_code=400, detail=f"Bank details required: {', '.join(missing)}")
        country = next((c for c in COUNTRIES if c["code"] == bank_details["country"]), None)
        if not country or country["currency"] != to_asset:
            raise HTTPException(status_code=400, detail=f"The selected country does not use {to_asset}")
    elif channel == "TO_EXTERNAL_WALLET":
        network = str(payload.get("network") or "").strip().lower()
        destination_wallet = str(payload.get("destination_wallet") or "").strip()
        if network not in NETWORKS:
            raise HTTPException(status_code=400, detail="Choose a supported settlement network")
        if not network_supports_asset(network, to_asset):
            raise HTTPException(status_code=400, detail=f"{to_asset} is not settled on {NETWORKS[network]['label']}")
        if not validate_wallet_address(network, destination_wallet):
            raise HTTPException(status_code=400, detail=f"That is not a valid {NETWORKS[network]['label']} address ({NETWORKS[network]['hint']})")

    rfq_id = f"RFQ-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.utcnow()
    rfq = {
        "id": rfq_id,
        "rfq_display": rfq_id,
        "customerId": str(current_user["_id"]),
        "customerName": current_user.get("businessName") or str(current_user["_id"]),
        "fromAsset": from_asset,
        "toAsset": to_asset,
        "side": str(payload.get("side", "SELL")).upper(),
        "amount": amount,
        "settlementChannel": channel,
        "bankDetails": bank_details,
        "destinationWallet": destination_wallet,
        "network": network,
        "channel": "DEALER",
        # Distinguishes merchant self-service RFQs from admin-initiated ones
        # in the same dealer_rfqs collection -- everything downstream
        # (quote/accept/execute/settle) treats them identically.
        "origin": "merchant_self_service",
        "status": "quote_ready",
        "createdAt": now,
        "updatedAt": now,
    }
    rfq["analysis"] = await AnalysisEngine(db).analyze(rfq)
    rfq["status"] = "quote_ready" if rfq["analysis"]["passed"] else "blocked"
    await db["dealer_rfqs"].insert_one(rfq)

    await db["admin_notifications"].insert_one({
        "category": "dealer_rfq",
        "type": "rfq_submitted_by_merchant",
        "title": f"New merchant RFQ: {rfq_id}",
        "message": f"{rfq['customerName']} requested {amount:,.2f} {from_asset} -> {to_asset}.",
        "route": "/admin/institutional-rfqs",
        "isRead": False,
        "sourceRfqId": rfq_id,
        "createdAt": now,
    })

    return {"status": "success", "rfq": _serialize_dealer_rfq_for_merchant(rfq)}


def _serialize_dealer_rfq_for_merchant(rfq: dict) -> dict:
    """
    _serialize_dealer_rfq returns the RFQ's `analysis` verbatim, which
    includes desk-internal figures (treasury inventory, liquidity routing/
    sources, risk exposure before/after) that a merchant's own client
    should never receive over the wire, regardless of whether the UI
    happens to render them -- the customer/compliance check groups (the
    only ones a merchant should see a reason from, e.g. why an RFQ was
    blocked) are kept.
    """
    serialized = _serialize_dealer_rfq(rfq)
    analysis = serialized.get("analysis")
    if isinstance(analysis, dict):
        serialized["analysis"] = {
            "passed": analysis.get("passed"),
            "customer": analysis.get("customer"),
            "compliance": analysis.get("compliance"),
        }
    return serialized


@router.get("/rfqs")
async def list_own_rfqs(db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    rfqs = await db["dealer_rfqs"].find({"customerId": str(current_user["_id"])}).sort("createdAt", -1).to_list(length=200)
    return {"status": "success", "rfqs": [_serialize_dealer_rfq_for_merchant(r) for r in rfqs]}


async def _get_own_rfq_or_404(db, rfq_id: str, current_user: dict) -> dict:
    rfq = await _fetch_dealer_rfq(db, rfq_id)
    if not rfq or str(rfq.get("customerId")) != str(current_user["_id"]):
        raise HTTPException(status_code=404, detail="RFQ not found")
    return rfq


@router.get("/rfqs/{rfq_id}")
async def get_own_rfq(rfq_id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    rfq = await _get_own_rfq_or_404(db, rfq_id, current_user)
    return {"status": "success", "rfq": _serialize_dealer_rfq_for_merchant(rfq)}


@router.post("/rfqs/{rfq_id}/accept")
async def merchant_accept_rfq(rfq_id: str, db=Depends(get_db), current_user: dict = Depends(get_verified_institutional_user)):
    """
    Accepting a quote is an AGREEMENT, not a payment -- it must never require
    the merchant to already hold a pre-funded balance (that would mean
    sending money before knowing the rate, which defeats the point of
    getting a quote first). No institutional_wallet_utils.lock_funds call
    here: the merchant's obligation to actually send `fromAsset` is only
    created once execute_dealer_rfq issues settlement-specific payment
    instructions, and only confirmed by treasury's existing
    confirm_customer_funds step (otc_admin.py's settlement action state
    machine) -- exactly the same discipline the admin-originated dealer flow
    already uses, just reached from the merchant portal instead of a
    dealer's manual entry.
    """
    await _get_own_rfq_or_404(db, rfq_id, current_user)
    result = await _accept_dealer_rfq_core(db, rfq_id, current_user)
    # Same desk-internal-figures scrub as the other merchant-facing RFQ
    # endpoints -- _accept_dealer_rfq_core is shared with the admin path and
    # returns the full analysis there on purpose.
    rfq = result.get("rfq")
    if isinstance(rfq, dict) and isinstance(rfq.get("analysis"), dict):
        analysis = rfq["analysis"]
        rfq["analysis"] = {"passed": analysis.get("passed"), "customer": analysis.get("customer"), "compliance": analysis.get("compliance")}
    return result


# --- Chat -- shared by both the merchant portal and the dealer's existing --
# --- RFQ workspace (DealerWorkspaceLive.tsx); access is either the RFQ's ---
# --- own customer, or any admin/dealer. -------------------------------------

async def _assert_rfq_participant(db, rfq_id: str, current_user: dict) -> dict:
    rfq = await _fetch_dealer_rfq(db, rfq_id)
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")
    is_owner = str(rfq.get("customerId")) == str(current_user.get("_id"))
    if not is_owner and not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Not a participant on this RFQ")
    return rfq


@router.get("/rfqs/{rfq_id}/messages")
async def get_rfq_messages(rfq_id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    # get_current_user_with_role, not get_current_user -- the latter is a
    # lightweight JWT-only dependency that never includes "role" (no DB
    # lookup), so _assert_rfq_participant's is_admin_role(current_user.get
    # ("role")) check always saw None and 403'd every admin, silently, on
    # both reading and sending. Only ever exercised by the merchant (RFQ
    # owner, passes on is_owner alone) until the admin chat UI existed to
    # surface it.
    await _assert_rfq_participant(db, rfq_id, current_user)
    messages = await db["dealer_rfq_messages"].find({"rfqId": rfq_id}).sort("createdAt", 1).to_list(length=500)
    for m in messages:
        m.pop("_id", None)
    return {"status": "success", "messages": messages}


@router.post("/rfqs/{rfq_id}/messages")
async def post_rfq_message(rfq_id: str, payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user_with_role)):
    rfq = await _assert_rfq_participant(db, rfq_id, current_user)
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 4000:
        raise HTTPException(status_code=400, detail="Message too long")

    is_sender_admin = is_admin_role(current_user.get("role"))
    now = datetime.utcnow()
    message = {
        "id": f"MSG-{uuid.uuid4().hex[:10].upper()}",
        "rfqId": rfq_id,
        "fromUserId": str(current_user.get("_id")),
        "fromRole": "dealer" if is_sender_admin else "merchant",
        "text": text,
        "createdAt": now,
    }
    await db["dealer_rfq_messages"].insert_one(dict(message))

    # Deliver live to whichever side didn't send it. broadcast_manager is an
    # in-memory, single-process pub/sub (see broadcast.py's own docstring) --
    # fine for the current single-instance deployment, would need a real
    # pub/sub (Redis) before running multiple backend instances.
    if is_sender_admin:
        recipient_id = rfq.get("customerId")
        if recipient_id:
            await broadcast_manager.send_user(str(recipient_id), {"type": "rfq_message", "rfqId": rfq_id, "message": message})
            try:
                await notify_user(
                    db, recipient_id, "dealer_rfq", "info",
                    "New message from your dealer",
                    text[:140],
                    extra={"rfqId": rfq_id, "route": f"/otc/rfqs/{rfq_id}"},
                )
            except Exception:
                pass
    else:
        # Merchant -> desk direction was previously dropped entirely (no
        # broadcast target, no notification) -- a merchant messaging on a
        # blocked/unquoted RFQ (no dealer assigned yet) had no way to reach
        # anyone. Land it in admin_notifications, the same general queue
        # rfq_submitted_by_merchant already uses, so any admin picks it up.
        try:
            await db["admin_notifications"].insert_one({
                "category": "dealer_rfq",
                "type": "rfq_message_from_merchant",
                "title": f"Message on {rfq_id} from {rfq.get('customerName') or 'a merchant'}",
                "message": text[:140],
                "route": "/admin/institutional-rfqs",
                "isRead": False,
                "sourceRfqId": rfq_id,
                "createdAt": now,
            })
        except Exception:
            pass
    if not is_sender_admin:
        # Notify admin desk generally -- individual dealer WS delivery would
        # need a stable admin-user routing convention this codebase doesn't
        # have yet (admin_notifications is the existing broad channel).
        await db["admin_notifications"].insert_one({
            "category": "dealer_rfq",
            "type": "rfq_message_from_merchant",
            "title": f"New message on {rfq_id}",
            "message": text[:200],
            "route": "/admin/institutional-rfqs",
            "isRead": False,
            "sourceRfqId": rfq_id,
            "createdAt": now,
        })

    return {"status": "success", "message": message}


# --- Ledger reads: Collections / Payouts / Transactions History -------------
# All three read the same institutional_ledger_entries collection -- written
# either by a treasury-confirmed standing-balance top-up
# (institutional_wallet_utils.credit_available, via the admin "credit wallet"
# tool) or, for the normal quote-then-fund settlement flow, by
# log_settlement_ledger_entry at confirm_customer_funds ("in") and reconciled
# ("out") in routes/otc_admin.py::act_on_dealer_settlement. Collections and
# Payouts are just directional filters over one merchant-scoped ledger, not
# three separate backend concepts.

def _serialize_ledger_entry(entry: dict) -> dict:
    entry = dict(entry)
    entry.pop("_id", None)
    return entry


async def _list_ledger_entries(db, user_id: str, *, direction: str | None, limit: int) -> list:
    query: dict = {"merchantId": str(user_id)}
    if direction:
        query["direction"] = direction
    entries = await db["institutional_ledger_entries"].find(query).sort("createdAt", -1).to_list(length=limit)
    return [_serialize_ledger_entry(e) for e in entries]


@router.get("/collections")
async def list_collections(limit: int = 100, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    """Inbound settlement legs credited to this merchant's wallet (the
    Pay-Ins-equivalent view) -- not a separate deposit rail, just the "in"
    side of the same ledger Payouts/Transactions History read."""
    entries = await _list_ledger_entries(db, str(current_user["_id"]), direction="in", limit=limit)
    return {"status": "success", "collections": entries}


@router.get("/payouts")
async def list_payouts(limit: int = 100, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    entries = await _list_ledger_entries(db, str(current_user["_id"]), direction="out", limit=limit)
    return {"status": "success", "payouts": entries}


@router.get("/transactions")
async def list_transactions(limit: int = 200, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    entries = await _list_ledger_entries(db, str(current_user["_id"]), direction=None, limit=limit)
    return {"status": "success", "transactions": entries}


# --- Settlements: merchant-scoped read of the admin-side settlement records -
# for this merchant's own accepted RFQs. No new collection -- reads
# dealer_settlements exactly like otc_admin.py::get_dealer_settlements does,
# just filtered to the requesting merchant instead of admin-global.

@router.get("/settlements")
async def list_own_settlements(db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    rfqs = await db["dealer_rfqs"].find(
        {"customerId": str(current_user["_id"])}, {"id": 1},
    ).to_list(length=500)
    rfq_ids = [r["id"] for r in rfqs]
    if not rfq_ids:
        return {"status": "success", "settlements": []}
    settlements = await db["dealer_settlements"].find(
        {"rfqId": {"$in": rfq_ids}},
    ).sort("createdAt", -1).to_list(length=200)
    return {"status": "success", "settlements": [
        {k: v for k, v in s.items() if k != "_id"} for s in settlements
    ]}


# --- Profile: account-level contact info, separate from KYB onboarding data -

@router.get("/profile")
async def get_own_account_profile(db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    profile = await db["institutional_profiles"].find_one({"userId": str(current_user["_id"])}) or {}
    # get_current_user is JWT-only and carries no email, so read it from the user doc.
    user_doc = await db.users.find_one(
        {"_id": ObjectId(str(current_user["_id"]))}, {"email": 1, "avatarUrl": 1, "createdAt": 1},
    ) or {}
    created = user_doc.get("createdAt")
    reviewed_by_name = None
    if profile.get("reviewedBy"):
        try:
            reviewer = await db.users.find_one({"_id": ObjectId(str(profile["reviewedBy"]))}, {"displayName": 1, "email": 1})
            reviewed_by_name = (reviewer or {}).get("displayName") or (reviewer or {}).get("email")
        except Exception:
            pass
    return {
        "status": "success",
        "profile": {
            "accountId": str(current_user["_id"]),
            "businessName": profile.get("businessName") or profile.get("legalName"),
            "email": user_doc.get("email"),
            "avatarUrl": user_doc.get("avatarUrl"),
            "memberSince": created.isoformat() if hasattr(created, "isoformat") else created,
            "contactName": profile.get("contactName"),
            "contactPhone": profile.get("contactPhone"),
            "onboardingStatus": profile.get("onboardingStatus", "not_started"),
            "reviewedByName": reviewed_by_name,
            "legalName": profile.get("legalName"),
            "companyType": profile.get("companyType"),
            "incorporationNumber": profile.get("incorporationNumber"),
            "countryOfIncorporation": profile.get("countryOfIncorporation"),
            "dateOfIncorporation": profile.get("dateOfIncorporation"),
            "taxNumber": profile.get("taxNumber"),
        },
    }


@router.put("/profile")
async def update_own_account_profile(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    fields = {k: payload.get(k) for k in ("contactName", "contactPhone") if k in payload}
    if not fields:
        return {"status": "success"}
    fields["updatedAt"] = datetime.utcnow()
    await db["institutional_profiles"].update_one({"userId": str(current_user["_id"])}, {"$set": fields})
    return {"status": "success"}


# --- Beneficiaries -----------------------------------------------------
# Saved payout destinations for this merchant -- bank or mobile money.
# Pure record-keeping, no money movement, so no compliance/treasury gate
# needed here (that happens at payout request time, below).

@router.get("/beneficiaries")
async def list_beneficiaries(db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    rows = await db["institutional_beneficiaries"].find(
        {"merchantId": str(current_user["_id"]), "saved": {"$ne": False}},
    ).sort("createdAt", -1).to_list(length=200)
    for r in rows:
        r.pop("_id", None)
    return {"status": "success", "beneficiaries": rows}


@router.post("/beneficiaries")
async def create_beneficiary(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    name = str(payload.get("name") or "").strip()
    channel = str(payload.get("channel") or "").strip().lower()
    currency = str(payload.get("currency") or "").strip().upper()
    if not name or channel not in {"bank", "mobile_money"} or not currency:
        raise HTTPException(status_code=400, detail="name, a valid channel (bank/mobile_money), and currency are required")

    doc = {
        "id": f"BEN-{uuid.uuid4().hex[:8].upper()}",
        "merchantId": str(current_user["_id"]),
        "name": name,
        "channel": channel,
        "currency": currency,
        "bankName": payload.get("bankName"),
        "accountNumber": payload.get("accountNumber"),
        "accountName": payload.get("accountName"),
        "phoneNumber": payload.get("phoneNumber"),
        "provider": payload.get("provider"),
        "beneficiaryType": str(payload.get("beneficiaryType") or "individual").lower(),
        "email": payload.get("email"),
        # One-time payouts store their destination as an unsaved beneficiary so
        # treasury still sees full bank details, without cluttering the
        # merchant's saved list.
        "saved": payload.get("saved", True) is not False,
        "createdAt": datetime.utcnow(),
    }
    if channel == "bank" and not (doc["accountNumber"] and doc["bankName"]):
        raise HTTPException(status_code=400, detail="bankName and accountNumber are required for a bank beneficiary")
    if channel == "mobile_money" and not doc["phoneNumber"]:
        raise HTTPException(status_code=400, detail="phoneNumber is required for a mobile money beneficiary")

    await db["institutional_beneficiaries"].insert_one(dict(doc))
    doc.pop("_id", None)
    return {"status": "success", "beneficiary": doc}


@router.delete("/beneficiaries/{beneficiary_id}")
async def delete_beneficiary(beneficiary_id: str, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    res = await db["institutional_beneficiaries"].delete_one(
        {"id": beneficiary_id, "merchantId": str(current_user["_id"])},
    )
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Beneficiary not found")
    return {"status": "success"}


# --- Payout requests -----------------------------------------------------
# A merchant asking treasury to pay a saved beneficiary out of their
# settled balance. Deliberately a REQUEST, not a direct transfer: every
# other money-movement path in this app (RFQ settlement, deposits) goes
# through a human treasury confirmation before funds actually move --
# letting a merchant trigger settle_bank_transfer/settle_mobilemoney_via_
# flutterwave straight from self-service would skip that discipline (and
# the ZIGRAM screening _screen_dealer_rfq runs on every RFQ) entirely for
# third-party beneficiary payouts, which is a bigger compliance surface
# than paying the merchant's own registered account.

@router.get("/payouts/requests")
async def list_payout_requests(db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    rows = await db["institutional_payout_requests"].find(
        {"merchantId": str(current_user["_id"])},
    ).sort("createdAt", -1).to_list(length=200)
    for r in rows:
        r.pop("_id", None)
    return {"status": "success", "requests": rows}


@router.post("/payouts/request")
async def create_payout_request(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    beneficiary_id = str(payload.get("beneficiaryId") or "").strip()
    amount = float(payload.get("amount", 0) or 0)
    if not beneficiary_id or amount <= 0:
        raise HTTPException(status_code=400, detail="beneficiaryId and a positive amount are required")

    beneficiary = await db["institutional_beneficiaries"].find_one(
        {"id": beneficiary_id, "merchantId": str(current_user["_id"])},
    )
    if not beneficiary:
        raise HTTPException(status_code=404, detail="Beneficiary not found")

    merchant_id = str(current_user["_id"])
    currency = beneficiary.get("currency")

    # Available balance minus payouts already awaiting treasury, so several
    # requests in a row can't collectively exceed what the merchant holds.
    wallet = await get_institutional_wallet(db, merchant_id)
    held = wallet.get(currency)
    available = float(held.get("available", 0) if isinstance(held, dict) else (held or 0))
    pending = await db["institutional_payout_requests"].find(
        {"merchantId": merchant_id, "currency": currency, "status": {"$in": ["pending_review", "approved"]}},
        {"amount": 1},
    ).to_list(length=500)
    committed = sum(float(p.get("amount", 0) or 0) for p in pending)
    if amount > available - committed:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient {currency} balance: {max(available - committed, 0):,.2f} available after pending payouts.",
        )

    profile = await db["institutional_profiles"].find_one({"userId": merchant_id}, {"legalName": 1, "businessName": 1})
    now = datetime.utcnow()
    doc = {
        "id": f"PR-{uuid.uuid4().hex[:8].upper()}",
        "merchantId": merchant_id,
        "merchantName": (profile or {}).get("legalName") or (profile or {}).get("businessName") or merchant_id,
        "beneficiaryId": beneficiary_id,
        "beneficiaryName": beneficiary.get("name"),
        "channel": beneficiary.get("channel"),
        "currency": currency,
        "amount": amount,
        "beneficiary": {k: beneficiary.get(k) for k in ("beneficiaryType", "bankName", "accountNumber", "accountName", "phoneNumber", "provider", "email")},
        "reference": str(payload.get("reference") or "").strip() or None,
        "batchId": str(payload.get("batchId") or "").strip() or None,
        "purpose": str(payload.get("purpose") or "").strip() or None,
        "status": "pending_review",
        "createdAt": now,
        "updatedAt": now,
    }
    await db["institutional_payout_requests"].insert_one(dict(doc))

    await db["admin_notifications"].insert_one({
        "category": "payout_request",
        "type": "payout_request_created",
        "title": f"Payout request {doc['id']} from {doc['merchantName']}",
        "message": f"{amount:,.2f} {doc['currency']} to beneficiary {beneficiary.get('name')} ({beneficiary.get('channel')}).",
        "route": "/admin/otc-requests",
        "isRead": False,
        "sourcePayoutRequestId": doc["id"],
        "createdAt": now,
    })

    doc.pop("_id", None)
    return {"status": "success", "request": doc}


# --- Funding requests ------------------------------------------------------
# "Fund Balance" on the wallet page -- there's no self-serve funding rail
# (no payment processor wired to institutional_wallets), so this creates a
# real record + notifies treasury rather than pretending to move money.
# Treasury fulfills it the same way any deposit is confirmed today: POST
# /api/admin/institutional-wallets/{user_id}/credit (already built).

@router.get("/wallet/funding-requests")
async def list_funding_requests(db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    rows = await db["institutional_funding_requests"].find(
        {"merchantId": str(current_user["_id"])},
    ).sort("createdAt", -1).to_list(length=100)
    for r in rows:
        r.pop("_id", None)
    return {"status": "success", "requests": rows}


@router.post("/wallet/fund-request")
async def create_funding_request(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    currency = str(payload.get("currency") or "").strip().upper()
    amount = float(payload.get("amount", 0) or 0)
    method = str(payload.get("method") or "bank_transfer").strip().lower()
    if not currency or amount <= 0:
        raise HTTPException(status_code=400, detail="currency and a positive amount are required")
    if method not in {"mobile_money", "bank_transfer"}:
        raise HTTPException(status_code=400, detail="method must be mobile_money or bank_transfer (crypto deposits are detected automatically)")
    phone = str(payload.get("phoneNumber") or "").strip()
    if method == "mobile_money" and not phone:
        raise HTTPException(status_code=400, detail="phoneNumber is required for a mobile money top-up")

    merchant_id = str(current_user["_id"])
    profile = await db["institutional_profiles"].find_one({"userId": merchant_id}, {"legalName": 1, "businessName": 1})
    now = datetime.utcnow()
    doc = {
        "id": f"FR-{uuid.uuid4().hex[:8].upper()}",
        "merchantId": merchant_id,
        "merchantName": (profile or {}).get("legalName") or (profile or {}).get("businessName") or merchant_id,
        "currency": currency,
        "amount": amount,
        "method": method,
        "country": payload.get("country"),
        "phoneNumber": phone or None,
        "provider": payload.get("provider"),
        "status": "pending",
        "createdAt": now,
    }
    await db["institutional_funding_requests"].insert_one(dict(doc))

    await db["admin_notifications"].insert_one({
        "category": "funding_request",
        "type": "funding_request_created",
        "title": f"Funding request from {doc['merchantName']}",
        "message": f"Requested {amount:,.2f} {currency} added to their wallet.",
        "route": "/admin/otc-requests",
        "isRead": False,
        "createdAt": now,
    })

    doc.pop("_id", None)
    return {"status": "success", "request": doc}


# --- Collection method requests --------------------------------------------
# "Request Virtual Account" / "Payment Link" equivalent -- there's no real
# banking-rail integration to auto-issue either, so this is an honest
# request treasury provisions manually, not a fake instant account number.

@router.get("/collections/requests")
async def list_collection_requests(db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    rows = await db["institutional_collection_requests"].find(
        {"merchantId": str(current_user["_id"])},
    ).sort("createdAt", -1).to_list(length=100)
    for r in rows:
        r.pop("_id", None)
    return {"status": "success", "requests": rows}


@router.post("/collections/request")
async def create_collection_request(payload: dict, db=Depends(get_db), current_user: dict = Depends(get_current_user)):
    currency = str(payload.get("currency") or "").strip().upper()
    method = str(payload.get("method") or "").strip().lower()
    if method in {"virtual_account", "payment_link"}:
        raise HTTPException(status_code=400, detail=f"{method.replace('_', ' ').title()} is coming soon")
    if not currency or method not in {"mobile_money", "crypto", "virtual_card"}:
        raise HTTPException(status_code=400, detail="currency and a valid method (mobile_money/crypto/virtual_card) are required")

    merchant_id = str(current_user["_id"])
    profile = await db["institutional_profiles"].find_one({"userId": merchant_id}, {"legalName": 1, "businessName": 1})
    now = datetime.utcnow()
    doc = {
        "id": f"CR-{uuid.uuid4().hex[:8].upper()}",
        "merchantId": merchant_id,
        "merchantName": (profile or {}).get("legalName") or (profile or {}).get("businessName") or merchant_id,
        "currency": currency,
        "method": method,
        "status": "pending",
        "createdAt": now,
    }
    await db["institutional_collection_requests"].insert_one(dict(doc))

    await db["admin_notifications"].insert_one({
        "category": "collection_request",
        "type": "collection_request_created",
        "title": f"Collection method request from {doc['merchantName']}",
        "message": f"Requested a {method.replace('_', ' ')} for {currency}.",
        "route": "/admin/otc-requests",
        "isRead": False,
        "createdAt": now,
    })

    doc.pop("_id", None)
    return {"status": "success", "request": doc}
