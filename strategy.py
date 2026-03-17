from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional

from config import BotConfig
from doma_api import Pool, Token, pick_token_usd_price


@dataclass
class Candidate:
    pool: Pool
    token_in: Token
    token_out: Token
    est_fee_bps: int
    est_price_impact_bps: Decimal
    score: Decimal
    reason: str


class StrategyEngine:
    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg

    def _estimate_impact_bps(
        self,
        trade_usd: Decimal,
        pool_tvl_usd: Decimal,
    ) -> Decimal:
        # Transparent, conservative approximation:
        # impact grows linearly with trade/pool_size and multiplied by 10 for safety.
        if pool_tvl_usd <= 0:
            return Decimal("99999")
        ratio = trade_usd / pool_tvl_usd
        return ratio * Decimal("10000") * Decimal("10")

    def build_candidates(
        self,
        pools: List[Pool],
        token_balances: Dict[str, Decimal],
        eth_price_usd: Decimal,
    ) -> List[Candidate]:
        candidates: List[Candidate] = []
        for pool in pools:
            if pool.tvl_usd < self.cfg.min_pool_tvl_usd:
                continue
            if pool.volume_24h_usd < self.cfg.min_pool_volume_24h_usd:
                continue

            if pool.fee_tier > self.cfg.max_fee_bps * 100:
                continue

            for token_in, token_out in ((pool.token0, pool.token1), (pool.token1, pool.token0)):
                if token_in.symbol not in self.cfg.allowed_symbols:
                    continue
                if token_out.symbol not in self.cfg.allowed_symbols:
                    continue

                wallet_amt = token_balances.get(token_in.address, Decimal("0"))
                token_usd = pick_token_usd_price(token_in, eth_price_usd)
                wallet_usd = wallet_amt * token_usd
                if wallet_usd < self.cfg.min_wallet_balance_usd:
                    continue

                est_impact = self._estimate_impact_bps(self.cfg.scoring_trade_usd, pool.tvl_usd)
                est_fee_bps = int(pool.fee_tier / 100)

                # Higher score is better: prefer high TVL/volume and lower costs.
                liquidity_term = (pool.tvl_usd / Decimal("1000")) + (pool.volume_24h_usd / Decimal("1000"))
                cost_term = Decimal(est_fee_bps) + est_impact
                score = liquidity_term / (cost_term + Decimal("1"))

                candidates.append(
                    Candidate(
                        pool=pool,
                        token_in=token_in,
                        token_out=token_out,
                        est_fee_bps=est_fee_bps,
                        est_price_impact_bps=est_impact,
                        score=score,
                        reason=(
                            f"tvl={pool.tvl_usd:.2f}, vol24h={pool.volume_24h_usd:.2f}, "
                            f"fee_bps={est_fee_bps}, impact_bps~{est_impact:.2f}"
                        ),
                    )
                )
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    def choose_best(
        self,
        pools: List[Pool],
        token_balances: Dict[str, Decimal],
        eth_price_usd: Decimal,
    ) -> Optional[Candidate]:
        candidates = self.build_candidates(pools, token_balances, eth_price_usd)
        if not candidates:
            return None
        return candidates[0]
