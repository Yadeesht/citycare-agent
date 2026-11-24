"""Token pricing. The host supplies the price table; agentcore hard-codes no model names."""

from __future__ import annotations

from dataclasses import dataclass

from .types import Usage


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_mult: float = 0.1     # cache reads cost this share of the input price
    cache_write_mult: float = 1.25   # cache writes cost this multiple of the input price


class Pricing:
    def __init__(self, prices: dict[str, ModelPrice] | None = None):
        self.prices = dict(prices or {})

    def knows(self, model: str) -> bool:
        return model in self.prices

    def cost(self, model: str, usage: Usage) -> float:
        """USD for one call. Unknown models cost 0; check `knows()` if that matters."""
        price = self.prices.get(model)
        if price is None:
            return 0.0
        per_in = price.input_per_mtok / 1_000_000
        per_out = price.output_per_mtok / 1_000_000
        return (
            usage.input_tokens * per_in
            + usage.output_tokens * per_out
            + usage.cache_read_tokens * per_in * price.cache_read_mult
            + usage.cache_write_tokens * per_in * price.cache_write_mult
        )
