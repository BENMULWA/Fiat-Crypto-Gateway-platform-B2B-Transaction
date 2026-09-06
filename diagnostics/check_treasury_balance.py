#!/usr/bin/env python3
"""
Diagnostic tool for Celo Treasury balance and withdrawal readiness.

This helps identify why withdrawals are failing by checking:
1. Treasury CELO balance (gas currency)
2. Treasury USDT/USDC balance
3. Current gas prices
4. Estimated transaction costs
"""

import asyncio
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from dotenv import load_dotenv

load_dotenv(override=True)

CELO_RPC = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
CHAIN_ID = 42220

w3 = Web3(Web3.HTTPProvider(CELO_RPC, request_kwargs={'timeout': 15}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

ASSET_CONTRACTS = {
    "cUSD": w3.to_checksum_address("0x765DE816845861e75A25fCA122bb6898B8B1282a"), 
    "USDC": w3.to_checksum_address("0xcebA9300f2b948710d2653dD7B07f33A8B32118C"), 
    "USDT": w3.to_checksum_address("0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e")  
}

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    }
]


def get_treasury_address():
    """Get the Treasury account address from environment."""
    pk = os.getenv("CELO_TREASURY_PK")
    if pk:
        try:
            clean_pk = pk if pk.startswith("0x") else f"0x{pk}"
            return w3.eth.account.from_key(clean_pk).address
        except Exception:
            pass
    return os.getenv("CELO_HOT_WALLET_ADDRESS", "0x6f7BeAb48EAfC47B89041899a35a0525a6A60F59")


def check_treasury_balances():
    """Check Treasury CELO and token balances."""
    treasury = get_treasury_address()
    
    print("\n" + "="*70)
    print("CELO TREASURY DIAGNOSTIC REPORT")
    print("="*70)
    print(f"\n📍 Treasury Address: {treasury}")
    
    # Check CELO (native token) balance
    try:
        celo_balance_wei = w3.eth.get_balance(treasury)
        celo_balance = celo_balance_wei / 1e18
        print(f"\n💰 CELO (Native) Balance: {celo_balance:.6f} CELO")
        print(f"   Raw (wei): {celo_balance_wei}")
    except Exception as e:
        print(f"\n❌ Error checking CELO balance: {e}")
        celo_balance = 0
    
    # Check gas prices
    try:
        gas_price_wei = w3.eth.gas_price
        gas_price_gwei = gas_price_wei / 1e9
        print(f"\n⛽ Current Gas Price: {gas_price_gwei:.2f} Gwei ({gas_price_wei} wei)")
    except Exception as e:
        print(f"\n❌ Error checking gas price: {e}")
        gas_price_wei = 0
    
    # Estimate gas cost for a typical transfer
    estimated_gas = 150000  # Standard ERC20 transfer
    estimated_cost_wei = gas_price_wei * estimated_gas
    estimated_cost_celo = estimated_cost_wei / 1e18
    
    print(f"\n📊 Estimated Cost per Withdrawal:")
    print(f"   Gas units: {estimated_gas}")
    print(f"   Cost: {estimated_cost_celo:.6f} CELO ({estimated_cost_wei} wei)")
    
    # Check token balances
    print(f"\n🪙 Token Balances:")
    for asset, contract_addr in ASSET_CONTRACTS.items():
        try:
            contract = w3.eth.contract(address=contract_addr, abi=ERC20_ABI)
            decimals = 18 if asset == "cUSD" else 6
            balance_raw = contract.functions.balanceOf(treasury).call()
            balance = balance_raw / (10 ** decimals)
            print(f"   {asset}: {balance:.6f} ({balance_raw} base units)")
        except Exception as e:
            print(f"   {asset}: ❌ Error - {e}")
    
    # Health status
    print(f"\n🔍 Health Status:")
    if celo_balance < estimated_cost_celo:
        print(f"   ❌ INSUFFICIENT GAS BALANCE")
        print(f"      Have: {celo_balance:.6f} CELO")
        print(f"      Need: {estimated_cost_celo:.6f} CELO per withdrawal")
        print(f"      Shortfall: {estimated_cost_celo - celo_balance:.6f} CELO")
    else:
        max_withdrawals = int(celo_balance / estimated_cost_celo)
        print(f"   ✅ SUFFICIENT GAS BALANCE")
        print(f"      Can execute ~{max_withdrawals} withdrawals at current gas price")
    
    print(f"\n💡 Recommendations:")
    if celo_balance < estimated_cost_celo:
        print(f"   1. Fund Treasury with at least {estimated_cost_celo:.6f} CELO")
        print(f"   2. Send CELO to: {treasury}")
        print(f"   3. Wait for transaction confirmation")
        print(f"   4. Retry withdrawal")
    else:
        print(f"   ✓ Treasury is ready for withdrawals")
    
    print("\n" + "="*70 + "\n")


if __name__ == "__main__":
    print("\n🔧 Running Treasury diagnostic...\n")
    
    try:
        check_treasury_balances()
    except Exception as e:
        print(f"\n❌ Diagnostic failed: {e}\n")
        raise
