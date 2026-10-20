"""
OpenClaw Quant Desk - Pydantic Schema Gate
File: src/validators.py
"""

from typing import Literal
from pydantic import BaseModel, Field, validator, ValidationError


class TradePlan(BaseModel):
    symbol: str
    direction: Literal["LONG", "SHORT"]
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float = Field(default=0.0)
    atr: float = Field(default=0.0)

    @validator('stop_loss')
    def validate_stop_loss(cls, v, values):
        direction = values.get('direction')
        entry_price = values.get('entry_price')
        if direction and entry_price is not None:
            if direction == "LONG" and v >= entry_price:
                raise ValueError(f"If LONG: stop_loss ({v}) must be < entry_price ({entry_price})")
            elif direction == "SHORT" and v <= entry_price:
                raise ValueError(f"If SHORT: stop_loss ({v}) must be > entry_price ({entry_price})")
        return v

    @validator('take_profit')
    def validate_take_profit(cls, v, values):
        direction = values.get('direction')
        entry_price = values.get('entry_price')
        if direction and entry_price is not None:
            if direction == "LONG" and v <= entry_price:
                raise ValueError(f"If LONG: take_profit ({v}) must be > entry_price ({entry_price})")
            elif direction == "SHORT" and v >= entry_price:
                raise ValueError(f"If SHORT: take_profit ({v}) must be < entry_price ({entry_price})")
        return v

    class Config:
        extra = "forbid"


class StrategyCandidate(BaseModel):
    strategy_name: str
    direction: Literal["LONG", "SHORT", "BOTH"] = "BOTH"
    stop_multiplier: float = Field(default=1.5, gt=0)
    profit_multiplier: float = Field(default=3.0, gt=0)
    min_sharpe: float = Field(default=1.0)
    max_drawdown_limit: float = Field(default=25.0)

    @validator('profit_multiplier')
    def validate_rr(cls, v, values):
        sl = values.get('stop_multiplier')
        if sl is not None and v < sl:
            raise ValueError(f"profit_multiplier ({v}) must be >= stop_multiplier ({sl}) for positive risk/reward.")
        return v

