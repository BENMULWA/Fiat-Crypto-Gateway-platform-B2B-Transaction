from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio

# Configuration and Database
from config import settings
from database import get_client, get_db

# The Background HFT Engine (The Brain)
try:
    from Brain_Engine.bot import hft_bot
except ImportError:
    hft_bot = None

# Routers (The Web Traffic)
from routes import (
    auth, dashboard, market_maker, trade, ramp, 
    airtime_ledger, general_ledger, rates, tokens, 
    cardano, treasury, retail, otc_admin, swap_engine, valora
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ==========================================
    # 1. STARTUP LOGIC
    # ==========================================
    print("🚀 Starting Meshex FastAPI Server...")
    
    client = get_client()
    bot_task = None
    
    try:
        # Verify Database Connection
        await client.admin.command("ping")
        print("✓ Connected to MongoDB")
        
        # Start the HFT Bot in the background
        if hft_bot:
            db = get_db()
            bot_task = asyncio.create_task(hft_bot.start(db))
            print("✓ HFT Background Bot Initiated")
            
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
        
    client.close()
    print("✓ MongoDB connection closed.")


# Initialize FastAPI Application
app = FastAPI(
    title="Meshex API",
    description="B2B arbitrage exchange — airtime · stablecoins · fiat ramps",
    version="1.0.0",
    lifespan=lifespan,
)

# Configure CORS (Dynamic allow-all patch to clear browser blocks instantly)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

# Health Check Routes
@app.get("/")
async def root():
    return {"message": "Mamlaka API is running", "status": "ok"}

@app.get("/health")
async def health():
    return {"status": "ok", "service": "meshex-api"}