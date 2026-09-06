from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import asyncio
import os

# Configuration and Database
from config import settings
from database import get_client, get_db

# The Background HFT Engine (The Brain)
try:
    from Brain_Engine.bot import hft_bot
except ImportError:
    hft_bot = None

# Persistent Celo per-user deposit detection (see workers/celo_deposit_watcher.py)
try:
    from workers.celo_deposit_watcher import celo_deposit_watcher_loop
except ImportError:
    celo_deposit_watcher_loop = None

# Persistent Cardano per-user deposit detection (see workers/cardano_deposit_watcher.py)
try:
    from workers.cardano_deposit_watcher import cardano_deposit_watcher_loop
except ImportError:
    cardano_deposit_watcher_loop = None

# Persistent Stellar per-user deposit detection (see workers/stellar_deposit_watcher.py)
try:
    from workers.stellar_deposit_watcher import stellar_deposit_watcher_loop
except ImportError:
    stellar_deposit_watcher_loop = None

# Routers (The Web Traffic)
from routes import (
    auth, dashboard, market_maker, trade, ramp, 
    airtime_ledger, general_ledger, rates, tokens, 
    cardano, treasury, retail, otc_admin, swap_engine, valora, stellar
)
from routes import realtime

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ==========================================
    # 1. STARTUP LOGIC
    # ==========================================
    print("🚀 Starting Meshex FastAPI Server...")
    
    client = get_client()
    bot_task = None
    airtel_timeout_task = None
    celo_watcher_task = None
    celo_watcher_stop = None
    cardano_watcher_task = None
    cardano_watcher_stop = None
    stellar_watcher_task = None
    stellar_watcher_stop = None

    try:
        # Verify Database Connection
        await client.admin.command("ping")
        print("✓ Connected to MongoDB")
        db = get_db()

        # Start the HFT Bot in the background
        if hft_bot:
            bot_task = asyncio.create_task(hft_bot.start(db))
            print("✓ HFT Background Bot Initiated")

        # Start the Celo deposit watcher (real-time, per-user deposit detection)
        if celo_deposit_watcher_loop:
            celo_watcher_stop = asyncio.Event()
            celo_watcher_task = asyncio.create_task(celo_deposit_watcher_loop(db, celo_watcher_stop))
            print("✓ Celo Deposit Watcher Initiated")

        # Start the Cardano deposit watcher (real-time, per-user deposit detection)
        if cardano_deposit_watcher_loop:
            cardano_watcher_stop = asyncio.Event()
            cardano_watcher_task = asyncio.create_task(cardano_deposit_watcher_loop(db, cardano_watcher_stop))
            print("✓ Cardano Deposit Watcher Initiated")

        # Start the Stellar deposit watcher (real-time, per-user deposit detection)
        if stellar_deposit_watcher_loop:
            stellar_watcher_stop = asyncio.Event()
            stellar_watcher_task = asyncio.create_task(stellar_deposit_watcher_loop(db, stellar_watcher_stop))
            print("✓ Stellar Deposit Watcher Initiated")

        async def expire_silent_airtel_entries():
            timeout_minutes = max(5, int(os.getenv("AIRTEL_PENDING_TIMEOUT_MINUTES", "15")))
            while True:
                cutoff = datetime.utcnow() - timedelta(minutes=timeout_minutes)
                await db["ramp_entries"].update_many(
                    {
                        "channel": "Mobile Money",
                        "status": {"$in": ["pending", "processing"]},
                        "providerReference": {"$exists": True, "$ne": ""},
                        "createdAt": {"$lte": cutoff},
                    },
                    {
                        "$set": {
                            "status": "failed",
                            "providerStatus": "NO_CALLBACK_TIMEOUT",
                            "error_reason": "No Airtel provider callback received before the request expired.",
                            "updatedAt": datetime.utcnow(),
                        },
                        "$push": {
                            "statusHistory": {
                                "state": "failed",
                                "at": datetime.utcnow(),
                                "source": "provider_callback_timeout",
                            }
                        },
                    },
                )
                await asyncio.sleep(60)

        airtel_timeout_task = asyncio.create_task(expire_silent_airtel_entries())
            
    except Exception as e:
        print(f"✗ Startup Error: {e}")
        
    # ==========================================
    # 2. RUNTIME (Server handles web requests here)
    # ==========================================
    yield 
    
    # ==========================================
    # 3. SHUTDOWN LOGIC
    # ==========================================
    print("🛑 Shutting down Meshex Server...")
    
    if bot_task and hft_bot:
        hft_bot.stop()
        await bot_task
        print("✓ HFT Background Bot safely stopped.")

    if celo_watcher_task and celo_watcher_stop:
        celo_watcher_stop.set()
        await celo_watcher_task
        print("✓ Celo Deposit Watcher safely stopped.")

    if cardano_watcher_task and cardano_watcher_stop:
        cardano_watcher_stop.set()
        await cardano_watcher_task
        print("✓ Cardano Deposit Watcher safely stopped.")

    if stellar_watcher_task and stellar_watcher_stop:
        stellar_watcher_stop.set()
        await stellar_watcher_task
        print("✓ Stellar Deposit Watcher safely stopped.")

    if airtel_timeout_task:
        airtel_timeout_task.cancel()
        try:
            await airtel_timeout_task
        except asyncio.CancelledError:
            pass
        
    client.close()
    print("✓ MongoDB connection closed.")


# Initialize FastAPI Application
app = FastAPI(
    title="Meshex API",
    description="B2B arbitrage exchange — airtime · stablecoins · fiat ramps",
    version="1.0.0",
    lifespan=lifespan,
)

# Credentialed CORS must be an explicit deployment allowlist. Configure
# CORS_ORIGINS in production with the exact retail and staff web origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register all API routes securely
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(market_maker.router)
app.include_router(trade.router)
app.include_router(ramp.router)
app.include_router(ramp.callback_router)
app.include_router(realtime.router)
app.include_router(airtime_ledger.router)
app.include_router(general_ledger.router)
app.include_router(rates.router)
app.include_router(tokens.router)
app.include_router(cardano.router)
app.include_router(treasury.router)
app.include_router(retail.router)
app.include_router(otc_admin.router)
app.include_router(swap_engine.router)
app.include_router(valora.router)
app.include_router(stellar.router)

# Health Check Routes
@app.get("/")
async def root():
    return {"message": "Mamlaka API is running", "status": "ok"}

@app.get("/health")
async def health():
    return {"status": "ok", "service": "meshex-api"}
