from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from config import settings
from database import get_client
from routes import auth, dashboard, market_maker, trade, ramp, airtime_ledger, general_ledger, rates, tokens, cardano


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Verify MongoDB connection on startup
    client = get_client()
    try:
        await client.admin.command("ping")
        print("✓ Connected to MongoDB")
    except Exception as e:
        print(f"✗ MongoDB connection failed: {e}")
    yield
    client.close()
    print("MongoDB connection closed.")


app = FastAPI(
    title="Meshex API",
    description="B2B arbitrage exchange — airtime · stablecoins · fiat ramps",
    version="1.0.0",
    lifespan=lifespan,
)

# Core Security Middlewares
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ⚡ Dynamic allow-all patch to clear browser blocks instantly
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register all routers
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


@app.get("/health")
async def health():
    return {"status": "ok", "service": "meshex-api"}
