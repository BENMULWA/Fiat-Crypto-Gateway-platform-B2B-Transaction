

from typing import Any

async def build_quote(rate_book: dict, amount: float, from_asset: str, to_asset: str, side: str, spread_bps: float) -> dict[str, Any]:
    from routes.treasury import compute_swap_quote_from_book
    base = compute_swap_quote_from_book(from_asset, to_asset, amount, rate_book)
    spread = max(float(spread_bps or 0), 0.0)
    if {from_asset, to_asset} == {"KES", "AIRT"}:
        return {
            "market_rate": base["market_rate"],
            "execution_rate": base["execution_rate"],
            "receive_amount": base["receive_amount"],
            "spread_bps": base["spread_bps"],
            "fee_amount": base["fee_amount"],
            "fee_currency": base.get("fee_currency", "KES"),
            "blended_cost": base["market_rate"],
            "expected_pnl": base["fee_amount"],
        }
    
# Maths to calculate the execution rate based on the side of the trade and the spread in basis points. The direction multiplier is calculated as 1 + spread/10000 for BUY trades and 1 - spread/10000 for SELL trades. The execution rate is then derived by multiplying the market rate by this direction multiplier. Finally, the receive amount is computed by multiplying the amount by the execution rate and rounding to four decimal places. 
# The function returns a dictionary containing the market rate, execution rate, receive amount, spread in basis points, blended cost, and expected profit or loss.  


# basis points = 1/ 100th of 1% = 0.001. For example, 100 basis points = 1% and 50 basis points = 0.5%. The spread in basis points is used to adjust the execution rate for the trade, either increasing it for BUY trades or decreasing it for SELL trades. This allows for a more accurate representation of the cost of executing the trade based on market conditions and the specified spread.
    direction = 1 + spread / 10000 if side == "BUY" else 1 - spread / 10000
    execution_rate = base["market_rate"] * direction
    receive_amount = round(amount * execution_rate, 4)
    return {
        "market_rate": base["market_rate"],
        "execution_rate": execution_rate,
        "receive_amount": receive_amount,
        "spread_bps": spread,
        "blended_cost": base["market_rate"],
        "expected_pnl": round(abs(receive_amount - base["market_receive_amount"]), 4),
    }
