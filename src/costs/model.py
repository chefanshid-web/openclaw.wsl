import math
from dataclasses import dataclass
from typing import Dict, Any, Tuple

@dataclass
class TransactionCostBreakdown:
    fee_bps: float
    half_spread_bps: float
    slippage_bps: float
    market_impact_bps: float
    funding_bps: float
    total_cost_bps: float
    is_eligible: bool
    rejection_reason: str = ""

class CostModel:
    def __init__(self,
                 taker_fee_bps: float = 4.0,     # 0.04%
                 maker_fee_bps: float = 2.0,     # 0.02%
                 half_spread_bps: float = 1.0,   # 0.01%
                 slippage_bps: float = 1.5,      # base slippage
                 impact_constant_y: float = 0.5):
        self.taker_fee_bps = taker_fee_bps
        self.maker_fee_bps = maker_fee_bps
        self.half_spread_bps = half_spread_bps
        self.slippage_bps = slippage_bps
        self.impact_constant_y = impact_constant_y

    def calculate_market_impact_bps(self, order_size_usd: float, daily_volume_usd: float, daily_volatility_pct: float) -> float:
        """
        Square-root law for market impact:
        I = Y * sigma * sqrt(Q / V)
        Returns impact in basis points (bps).
        """
        if daily_volume_usd <= 0 or order_size_usd <= 0:
            return 0.0
        
        ratio = order_size_usd / daily_volume_usd
        # Daily volatility converted to bps (1% = 100 bps)
        sigma_bps = daily_volatility_pct * 10000.0
        impact_bps = self.impact_constant_y * sigma_bps * math.sqrt(ratio)
        return float(impact_bps)

    def evaluate_cost(self,
                      expected_move_bps: float,
                      order_size_usd: float,
                      daily_volume_usd: float,
                      daily_volatility_pct: float = 0.03,
                      is_taker: bool = True,
                      holding_hours: float = 4.0,
                      funding_rate_8h: float = 0.0001) -> TransactionCostBreakdown:
        """
        Calculates all frictional costs and evaluates the eligibility gate:
        expected_move > 2 * total_cost
        """
        fee_bps = self.taker_fee_bps if is_taker else self.maker_fee_bps
        impact_bps = self.calculate_market_impact_bps(order_size_usd, daily_volume_usd, daily_volatility_pct)
        funding_bps = abs(funding_rate_8h) * (holding_hours / 8.0) * 10000.0
        
        # Round-trip cost = 2 * (fee + spread + slippage + impact) + funding
        total_cost_bps = 2.0 * (fee_bps + self.half_spread_bps + self.slippage_bps + impact_bps) + funding_bps
        
        # Eligibility Gate: Rule - expected_move must exceed 2x total cost
        required_edge_bps = 2.0 * total_cost_bps
        is_eligible = expected_move_bps > required_edge_bps
        rejection_reason = "" if is_eligible else f"Expected move ({expected_move_bps:.1f} bps) < 2x cost ({required_edge_bps:.1f} bps)"
        
        return TransactionCostBreakdown(
            fee_bps=fee_bps,
            half_spread_bps=self.half_spread_bps,
            slippage_bps=self.slippage_bps,
            market_impact_bps=impact_bps,
            funding_bps=funding_bps,
            total_cost_bps=total_cost_bps,
            is_eligible=is_eligible,
            rejection_reason=rejection_reason
        )
