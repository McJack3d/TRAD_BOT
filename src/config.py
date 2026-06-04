"""Typed configuration loaded from YAML + .env.

Single source of truth for trading parameters, risk limits, and system
settings. Any module that needs a parameter reads it from here so the
config file is the only knob.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Mode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    DRY_RUN = "dry_run"
    LIVE = "live"


class SymbolConfig(BaseModel):
    """A single tradable symbol pair (spot + perp use the same base)."""

    spot: str
    perp: str
    min_qty: Decimal = Decimal("0.0001")
    tick_size: Decimal = Decimal("0.01")
    qty_step: Decimal = Decimal("0.0001")


class StrategyConfig(BaseModel):
    entry_funding_threshold: Decimal = Decimal("0.0002")  # 0.02% per 8h
    exit_funding_threshold: Decimal = Decimal("0.00005")  # 0.005% per 8h
    min_dwell_hours: int = 24
    perp_leverage: int = Field(2, ge=1, le=5)
    equal_weight: bool = True

    @field_validator("entry_funding_threshold", "exit_funding_threshold")
    @classmethod
    def positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("funding threshold must be positive")
        return v


class RiskConfig(BaseModel):
    max_gross_notional_pct: Decimal = Decimal("0.50")  # 50% of equity per symbol cap
    max_total_exposure_pct: Decimal = Decimal("1.00")  # 100% across all symbols
    liquidation_halt_pct: Decimal = Decimal("0.20")  # flatten if dist < 20%
    margin_top_up_pct: Decimal = Decimal("0.30")  # top up if dist < 30%
    pre_trade_min_liq_distance_pct: Decimal = Decimal("0.30")
    daily_loss_stop_pct: Decimal = Decimal("0.02")  # 2% of starting equity
    cumulative_loss_stop_pct: Decimal = Decimal("0.10")  # 10% of starting equity
    max_orders_per_minute: int = 10
    max_clock_drift_ms: int = 100
    reconciliation_stale_seconds: int = 300


class ReconciliationConfig(BaseModel):
    interval_seconds: int = 60
    position_size_tolerance_pct: Decimal = Decimal("0.001")  # 0.1%
    balance_tolerance_usdt: Decimal = Decimal("1.0")


class FeesConfig(BaseModel):
    spot_taker_bps: Decimal = Decimal("4.0")  # 0.04%
    perp_taker_bps: Decimal = Decimal("4.0")
    assumed_slippage_bps: Decimal = Decimal("2.0")


class MonitoringConfig(BaseModel):
    telegram_enabled: bool = True
    email_enabled: bool = True
    daily_digest_utc_hour: int = 8
    weekly_digest_utc_dow: int = 0  # Monday
    ws_drop_alert_seconds: int = 30


class BacktestConfig(BaseModel):
    start: str = "2020-01-01"
    end: str | None = None
    initial_equity_eur: Decimal = Decimal("1000")
    walk_forward_train_months: int = 6
    walk_forward_test_months: int = 1
    oos_split: Decimal = Decimal("0.30")
    data_dir: str = "data/history"


class BotConfig(BaseModel):
    """Top-level config tree."""

    mode: Mode = Mode.PAPER
    # Account is funded and settled in USDT. The legacy YAML/key
    # `starting_equity_eur` is still accepted (see the validator below)
    # so old configs keep working, but the canonical name is USDT to
    # match what the bot actually trades and snapshots.
    starting_equity_usdt: Decimal = Decimal("1000")
    symbols: list[SymbolConfig]
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    reconciliation: ReconciliationConfig = Field(default_factory=ReconciliationConfig)
    fees: FeesConfig = Field(default_factory=FeesConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_equity_key(cls, data: Any) -> Any:
        """Map the legacy `starting_equity_eur` key onto the canonical
        `starting_equity_usdt`. Accepts both YAML files and kwargs so
        existing configs and tests keep working unchanged."""
        if isinstance(data, dict) and "starting_equity_eur" in data:
            data.setdefault("starting_equity_usdt", data["starting_equity_eur"])
            data.pop("starting_equity_eur", None)
        return data

    @property
    def starting_equity_eur(self) -> Decimal:
        """Backwards-compatible alias. The account is USDT-denominated;
        this returns the same value as `starting_equity_usdt`."""
        return self.starting_equity_usdt

    @classmethod
    def from_yaml(cls, path: str | Path) -> BotConfig:
        with open(path) as f:
            data: dict[str, Any] = yaml.safe_load(f)
        return cls.model_validate(data)


class Secrets(BaseSettings):
    """Loaded from environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = True

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    email_smtp_host: str = "smtp.gmail.com"
    email_smtp_port: int = 587
    email_username: str = ""
    email_password: str = ""
    email_from: str = ""
    email_to: str = ""

    bot_env: str = "paper"
    bot_config: str = "config/paper.yaml"
    bot_db_path: str = "data/bot.db"
    bot_log_level: str = "INFO"
