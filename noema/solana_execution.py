"""Live Jupiter venue adapter for trench desk entries and exits.

Paper desks stay on the simulated cost model. This module runs only when the
owner live gate is on. It never reads ``NOEMA_SOLANA_SIGNER_SECRET``; the
isolated signer does.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

import httpx

from .economic_ledger import EconomicEvent, EconomicLedger
from .solana_live_gate import DeskSwapRejected
from .solana_swap import WSOL_MINT, _endpoint, _rpc, inspect_mint_account
from .trench_desks import LegFill, Mark
from .trench_models import TokenControlState, TrenchFeatures
from .trench_risk import assess_trench_candidate
from .wallet_intents import WalletIntent
from .wallet_types import Chain

_LAMPORTS = Decimal(1_000_000_000)


def _sol_price() -> Decimal:
    raw = os.getenv("NOEMA_SOLANA_PRICE_USD", "").strip()
    try:
        price = Decimal(raw)
    except InvalidOperation:
        raise DeskSwapRejected("solana price is not configured") from None
    if not price.is_finite() or price <= 0:
        raise DeskSwapRejected("solana price is not configured")
    return price


def _slippage_bps() -> int:
    raw = os.getenv("NOEMA_SOLANA_MAX_SLIPPAGE_BPS", "100").strip() or "100"
    try:
        value = int(raw, 10)
    except ValueError:
        raise DeskSwapRejected("signer limit configuration is invalid") from None
    if not 1 <= value <= 2_000:
        raise DeskSwapRejected("slippage exceeds signer maximum")
    return value


def _features() -> TrenchFeatures:
    return TrenchFeatures(
        age_seconds=0.0, observations=1, return_fraction=0.0, max_drawdown_fraction=0.0,
        liquidity_growth_fraction=0.0, signed_flow_imbalance=None, buyer_growth_fraction=None,
        buyer_acceleration=None, participation_balance=None, organic_buyer_share=None,
        organic_volume_fraction=None, top_holder_fraction=None, top5_holder_fraction=None,
        holder_hhi=None, creator_supply_fraction=None, organic_score=None,
        wash_trade_probability=None,
    )


def fetch_mint_info(mint: str) -> dict[str, Any]:
    """Public mint account read. Refuses freeze authority, mint authority, and unsafe extensions."""
    rpc_url = _endpoint("NOEMA_SOLANA_RPC_URL", None, missing="Solana RPC endpoint is not configured")
    try:
        with httpx.Client(timeout=15.0) as client:
            account = _rpc(
                client, rpc_url, "getAccountInfo",
                [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            )
        parsed = inspect_mint_account(account.get("value") if isinstance(account, dict) else None)
    except RuntimeError as exc:
        raise DeskSwapRejected(str(exc)) from None
    return {
        "decimals": parsed["decimals"],
        "control": TokenControlState(
            token_program=parsed["owner"],
            mint_authority_present=False,
            freeze_authority_present=False,
            permanent_delegate_present=False,
            transfer_hook_present=False,
            transfer_fee_bps=0,
        ),
    }


def record_swap_fill(db_path: str, fill: dict[str, Any], *, desk_id: str, mint: str) -> dict[str, Any]:
    """Append the confirmed on-chain amounts. Duplicate signatures are idempotent."""
    sol_delta = Decimal(int(fill["sol_delta_lamports"]))
    price = Decimal(str(fill["sol_price_usd"]))
    amount_usd = abs(sol_delta) / _LAMPORTS * price
    occurred = datetime.fromisoformat(str(fill["price_timestamp"]))
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)
    event = EconomicEvent(
        provider="wallet:solana",
        event_type="dex_swap_fill",
        occurred_at=occurred,
        currency="SOL",
        amount=sol_delta,
        amount_usd=amount_usd,
        reconciliation_state="RECONCILED",
        value_state="realized",
        capital_class="unclassified_wallet_flow",
        confidence_state="provider_confirmed",
        completeness_state="complete",
        external_reference_id=str(fill["signature"]),
        mission_id=f"trench-desk:{desk_id}",
        strategy_id=desk_id,
        lane="trench_desk",
        owned_account_id=str(fill.get("address") or ""),
        evidence={
            "decimals": 9,
            "price_source": "NOEMA_SOLANA_PRICE_USD",
            "price_timestamp": occurred.isoformat(),
            "valuation_basis": "event_time",
            "price_usd": format(price, "f"),
            "mint": mint,
            "side": fill.get("side"),
            "token_delta_raw": str(fill.get("token_delta_raw")),
            "token_decimals": int(fill["token_decimals"]),
            "fee_lamports": int(fill["fee_lamports"]),
            "desk_id": desk_id,
            "idempotency_key": fill.get("idempotency_key"),
            "signature": fill.get("signature"),
        },
    )
    return EconomicLedger(db_path).record_event(event)


class JupiterDeskExecutor:
    """Turn a desk leg into one signer swap and reconcile the real token amount."""

    def __init__(self, db_path: str, *, signer: Any | None = None, gateway: Any | None = None,
                 mint_info: Any | None = None) -> None:
        self.db_path = db_path
        self.signer = signer
        self.gateway = gateway
        self.mint_info = mint_info or fetch_mint_info

    def buy(self, *, desk_id: str, mint: str, symbol: str, leg: str, notional_usd: float,
            mark: Mark, reason: str, idempotency_key: str) -> LegFill:
        del symbol, reason
        info = self.mint_info(mint)
        assessment = assess_trench_candidate(
            _features(), info["control"], current_liquidity_usd=mark.liquidity_usd,
        )
        if assessment.disposition == "quarantine":
            raise DeskSwapRejected("trench candidate quarantined")
        price = _sol_price()
        slippage = _slippage_bps()
        lamports = int((Decimal(str(notional_usd)) / price * _LAMPORTS).to_integral_value(ROUND_DOWN))
        if lamports <= 0 or mark.price_usd <= 0:
            raise DeskSwapRejected("swap request is invalid")
        expected = Decimal(str(notional_usd)) / Decimal(str(mark.price_usd))
        min_ui = expected * (Decimal(1) - Decimal(slippage) / Decimal(10_000))
        min_out = int((min_ui * (Decimal(10) ** int(info["decimals"]))).to_integral_value(ROUND_DOWN))
        if min_out <= 0:
            raise DeskSwapRejected("minimum output is invalid")
        fill = self._execute(
            desk_id=desk_id, mint=mint, leg=leg, side="buy", amount=lamports,
            min_out=min_out, slippage=slippage, notional=Decimal(str(notional_usd)),
            idempotency_key=idempotency_key,
        )
        return _leg_fill(fill, mark=mark, side="buy")

    def sell(self, *, desk_id: str, mint: str, symbol: str, leg: str, qty: float,
             cost_usd: float, mark: Mark, reason: str, idempotency_key: str,
             opened_at: str | None = None) -> LegFill:
        del symbol, cost_usd, reason, opened_at
        info = self.mint_info(mint)
        price = _sol_price()
        slippage = _slippage_bps()
        raw = int((Decimal(str(qty)) * (Decimal(10) ** int(info["decimals"]))).to_integral_value(ROUND_DOWN))
        if raw <= 0 or mark.price_usd <= 0:
            raise DeskSwapRejected("swap request is invalid")
        gross_usd = Decimal(str(qty)) * Decimal(str(mark.price_usd))
        min_lamports = int(
            (gross_usd / price * (Decimal(1) - Decimal(slippage) / Decimal(10_000)) * _LAMPORTS)
            .to_integral_value(ROUND_DOWN)
        )
        if min_lamports <= 0:
            raise DeskSwapRejected("minimum output is invalid")
        fill = self._execute(
            desk_id=desk_id, mint=mint, leg=leg, side="sell", amount=raw,
            min_out=min_lamports, slippage=slippage, notional=gross_usd,
            idempotency_key=idempotency_key,
        )
        return _leg_fill(fill, mark=mark, side="sell")

    def _execute(self, *, desk_id: str, mint: str, leg: str, side: str, amount: int,
                 min_out: int, slippage: int, notional: Decimal, idempotency_key: str) -> dict[str, Any]:
        from .execution_gateway import ExecutionGateway

        gateway = self.gateway or ExecutionGateway(self.db_path)
        signer = self._signer()
        intent = WalletIntent(
            intent_id=idempotency_key,
            chain=Chain.SOLANA,
            venue="solana",
            action="dex_swap",
            asset_in=WSOL_MINT if side == "buy" else mint,
            asset_out=mint if side == "buy" else WSOL_MINT,
            notional_usd=notional,
            expected_slippage_bps=Decimal(slippage),
            mission_id=f"trench-desk:{desk_id}",
            evidence_ids=(f"trench-desk:{desk_id}:{mint}:{leg}:{side}",),
            amount_atomic=amount,
            minimum_output_atomic=min_out,
            strategy_id=desk_id,
        )

        def handler() -> dict[str, Any]:
            return signer.jupiter_swap_sync(
                side=side, mint=mint, amount=amount, max_slippage_bps=slippage,
                min_out=min_out, idempotency_key=idempotency_key,
            )

        result = gateway.submit_dex_swap(intent=intent, handler=handler)
        if result.get("status") == "rejected":
            reasons = result.get("reasons") or ["solana live swaps are disabled"]
            raise DeskSwapRejected(str(reasons[0]))
        if result.get("status") != "confirmed":
            raise DeskSwapRejected("swap fill could not be reconciled")
        record_swap_fill(self.db_path, result, desk_id=desk_id, mint=mint)
        return result

    def _signer(self) -> Any:
        if self.signer is not None:
            return self.signer
        from .wallet_signer import LocalSolanaWalletSigner

        self.signer = LocalSolanaWalletSigner()
        return self.signer


def _leg_fill(fill: dict[str, Any], *, mark: Mark, side: str) -> LegFill:
    decimals = int(fill["token_decimals"])
    raw = abs(int(fill["token_delta_raw"]))
    if raw <= 0:
        raise DeskSwapRejected("swap fill could not be reconciled")
    qty = Decimal(raw) / (Decimal(10) ** decimals)
    price = Decimal(str(fill["sol_price_usd"]))
    sol_delta = Decimal(int(fill["sol_delta_lamports"]))
    fee_usd = Decimal(int(fill["fee_lamports"])) / _LAMPORTS * price
    spent = abs(sol_delta) / _LAMPORTS * price
    mark_value = qty * Decimal(str(mark.price_usd))
    if side == "buy":
        notional = spent
        slip = max(Decimal(0), (spent - fee_usd) - mark_value)
        fill_price = spent / qty
    else:
        notional = sol_delta / _LAMPORTS * price
        slip = max(Decimal(0), mark_value - (notional + fee_usd))
        fill_price = abs(notional) / qty
    return LegFill(
        qty=float(qty), fill_price=float(fill_price), fee_usd=float(fee_usd),
        slippage_usd=float(slip), notional_usd=float(notional), paper=0,
        signature=str(fill["signature"]), amount_in_raw=str(fill["amount_in"]),
        amount_out_raw=str(fill["amount_out"]), idempotency_key=str(fill["idempotency_key"]),
    )
