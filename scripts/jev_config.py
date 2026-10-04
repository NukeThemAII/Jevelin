#!/usr/bin/env python3
"""Shared M0/M3 plumbing + the M3 v2 config home. Stdlib only (PyYAML in loader).

Holds:
  * the named execution-cost constants (M0 / F-P0-1) and ExecutionConfig;
  * every threshold dataclass (M3): RiskConfig / PerpsConfig gate thresholds,
    RegimeConfig, FanoutConfig, CacheConfig, BurstConfig, MarketConfig —
    frozen, with the DESIGN (docs/V2-DESIGN.md B.3) numbers as their defaults;
  * V2Config + ``load_config()``: config/v2.yaml overrides the dataclass
    defaults (single source of truth: dataclass defaults == shipped yaml).
    Unknown keys warn to stderr (not fatal); bad types/values raise
    ``ConfigError`` at startup with a clear message;
  * the per-gate veto flags (F-P1-4), decision-id generation (F-P1-3) and the
    atomic JSON writer (F-P2) used by both paper books, both gate layers and
    the supervisors.

No trading logic lives here — thresholds are data; decisions are code.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import IntFlag
from pathlib import Path

# -- execution-cost defaults (M0 / F-P0-1) ---------------------------------
# Real Binance rates: spot 0.10% per side (maker=taker for the operator),
# USDT-M VIP0 taker 0.05% per side. Slippage 5 bps per side, 0.0 = perfect
# limit fills (explicitly allowed).
SPOT_FEE_RATE = 0.001
PERPS_TAKER_FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0005

CONFIG_VERSION = 4
# Loader accepts the previous schema too (v3 files predate the M6 scout
# section and load with scout defaults); the file's declared version is
# preserved as-is in V2Config.config_version.
SUPPORTED_CONFIG_VERSIONS = (3, CONFIG_VERSION)

# M5 default pair universe (config/v2.yaml ``pairs``; --pairs overrides).
DEFAULT_PAIRS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

# Default config file (absolute: the CLI works from any cwd).
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "v2.yaml"


class ConfigError(Exception):
    """Bad config file: wrong type/value, bad shape, or unsupported version."""


@dataclass(frozen=True)
class ExecutionConfig:
    spot_fee_rate: float = SPOT_FEE_RATE
    perps_taker_fee_rate: float = PERPS_TAKER_FEE_RATE
    slippage_rate: float = SLIPPAGE_RATE


@dataclass(frozen=True)
class RegimeConfig:
    """jev_regime classifier parameters (docs/V2-DESIGN.md B.3)."""
    vol_window: int = 16          # log-return sample for 15m realized vol
    atr_period: int = 14          # ATR(14) on 15m = vol expectation base
    vol_ratio_threshold: float = 0.8   # realized/ATR below this -> compressed
    donchian_period: int = 20     # Donchian(20) width on 15m
    donchian_history: int = 50    # trailing widths for the width percentile
    narrow_percentile: float = 0.4    # width pct below this -> narrow
    ema_period: int = 9           # short EMA on 1h closes
    ema_slope_bars: int = 3       # slope taken over the last 3 EMA points
    flat_slope: float = 0.001     # |slope|/bar below this -> flat (~0.1%/bar)


@dataclass(frozen=True)
class FanoutConfig:
    """Whipsaw self-consistency fan-out (B.3): 2nd Jev sample in the coin-flip band."""
    band_low: float = 0.40
    band_high: float = 0.60


@dataclass(frozen=True)
class CacheConfig:
    """M2 decision cache."""
    cache_min_move: float = 0.0005    # < 5 bp reuses the verdict
    cache_ttl: float = 1800.0         # verdict reuse window (s)


@dataclass(frozen=True)
class BurstConfig:
    """M2 burst trigger."""
    burst_threshold: float = 0.003    # |1-min return| forcing a score
    burst_trades: int = 150           # 1-min trade count forcing a score
    burst_cycles: int = 2             # extra forced slow cycles after a spike


@dataclass(frozen=True)
class MarketConfig:
    """Supervisor market-data fetch policy (M3 regime inputs)."""
    ohlcv_ttl_seconds: float = 60.0   # per-timeframe OHLCV cache TTL
    ohlcv_15m_limit: int = 60
    ohlcv_1h_limit: int = 120


@dataclass(frozen=True)
class RiskConfig:
    """Spot gate thresholds (jev_gates) — design numbers from B.3.

    Note: per B.3's exit rule, signal exits are the dump hysteresis
    (exit_min_dump x exit_consecutive_cycles) OR the single-tick bar
    (exit_hard_dump); stops/liquidation always exit regardless. There is no
    separate exhaustion exit in v2 (v1's exit_exhaustion is superseded).
    """
    max_position_fraction: float = 0.20
    entry_min_pump: float = 65.0
    entry_max_whipsaw: float = 0.45
    entry_max_exhaustion: float = 0.55
    min_confidence: float = 0.65
    entry_phases: tuple = ("breakout", "accumulation")
    counter_trend: str = "block"      # "block" | "allow" (config-only, no code change)
    exit_min_dump: float = 65.0
    exit_hard_dump: float = 75.0
    exit_consecutive_cycles: int = 2
    min_hold_cycles: int = 3
    cooldown_seconds: int = 900
    daily_loss_limit_pct: float = 5.0
    tier_thresholds: tuple = (0.70, 0.85)        # confidence band edges
    tier_fractions: tuple = (0.60, 0.80, 1.00)   # fraction of cap per band


@dataclass(frozen=True)
class PerpsConfig:
    """Perps gate thresholds (jev_perps) — mirror of RiskConfig + short side.

    Money semantics (leverage cap, margin cap, stop/liq/funding) are unchanged
    since M0; only decision thresholds follow B.3. Exit rule per B.3: pump
    hysteresis mirrors dump (exit_min_pump x exit_consecutive_cycles, or the
    exit_hard_pump single-tick bar); stops/liq always allowed.
    """
    max_leverage: float = 3.0
    max_margin_fraction: float = 0.10
    entry_min_pump: float = 65.0
    short_min_dump: float = 65.0
    entry_max_whipsaw: float = 0.45
    entry_max_exhaustion: float = 0.55
    min_confidence: float = 0.65
    entry_phases: tuple = ("breakout", "accumulation")
    # Short-side bars (2026-10-04): the phase + whipsaw questions are phrased
    # for the upside ("price breaking up", "is this breakout a fakeout?"), so
    # shorts get their own. Defaults mirror the long side (no behavior change).
    short_entry_phases: tuple = ("breakout", "accumulation")
    short_max_whipsaw: float = 0.45
    counter_trend: str = "block"
    exit_min_dump: float = 65.0
    exit_hard_dump: float = 75.0
    exit_min_pump: float = 65.0
    exit_hard_pump: float = 75.0
    exit_consecutive_cycles: int = 2
    min_hold_cycles: int = 3
    cooldown_seconds: int = 900
    daily_loss_limit_pct: float = 5.0
    stop_loss_pct: float = 2.0
    max_abs_funding_pct: float = 0.01  # percent per 8h -> 0.0001 as a rate
    tier_thresholds: tuple = (0.70, 0.85)
    tier_fractions: tuple = (0.60, 0.80, 1.00)
    # Execution costs (M0 / F-P0-1): Binance USDT-M VIP0 taker + 5 bps slippage.
    taker_fee_rate: float = PERPS_TAKER_FEE_RATE
    slippage_rate: float = SLIPPAGE_RATE


@dataclass(frozen=True)
class ScoutConfig:
    """M6 CoinGecko discovery scout (B.4a) — WHERE to look, never WHAT to do.

    ``enabled`` is the config-level kill-switch: the supervisor's opt-in
    ``--universe scout`` mode and ``jev_scout.py`` refuse to run while it is
    false (falling back to the static universe / exiting cleanly).
    """
    enabled: bool = True
    quote: str = "USDT"
    min_24h_vol_usd: float = 5_000_000.0   # Binance 24h quote volume floor
    min_listing_age_days: float = 30.0     # first daily candle age floor
    max_pairs: int = 5                     # cap on the final ranked list
    interval_seconds: float = 3600.0       # expected pass cadence (staleness)
    # M6+ Pool B (market-movers seed pool — spec fallback scan: top-250 by
    # volume, top movers by |24h change| kept; junk-density lower than ideal);
    # gainers_enabled=false -> Pool A only, exactly the M6 behavior.
    gainers_enabled: bool = True
    gainers_per_page: int = 100  # Pool B pool width (kept <= 50 in fallback)


@dataclass(frozen=True)
class PortfolioConfig:
    """M5 portfolio risk thresholds (scripts/jev_risk.py) — B.3 / M5 spec.

    Every cap is a fraction; the portfolio risk layer enforces them in
    deterministic code — Jev never sees them.
    """
    pair_spot_cap: float = 0.30           # per-pair spot notional <= 30% pair spot equity
    pair_perps_margin_cap: float = 0.10   # per-pair perps margin <= 10% pair perps equity
    basket_long_cap: float = 0.40         # total long notional <= 40% total equity
    basket_short_cap: float = 0.20        # total short notional <= 20% total equity
    global_daily_loss: float = -0.05      # portfolio daily PnL <= -5% -> block entries
    drawdown_halt: float = 0.10           # dd from running peak >= 10% -> block entries
    drawdown_recover: float = 0.05        # halt clears only below 5% dd (hysteresis)
    min_position_pct: float = 0.01        # capacity < 1% equity -> veto (no dust)


@dataclass(frozen=True)
class V2Config:
    """Resolved v2 config: one frozen section per concern (config/v2.yaml)."""
    config_version: int = CONFIG_VERSION
    pairs: tuple = DEFAULT_PAIRS
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    fanout: FanoutConfig = field(default_factory=FanoutConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    burst: BurstConfig = field(default_factory=BurstConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    spot: RiskConfig = field(default_factory=RiskConfig)
    perps: PerpsConfig = field(default_factory=PerpsConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    scout: ScoutConfig = field(default_factory=ScoutConfig)

    def to_dict(self) -> dict:
        """Nested plain dict (tuples -> lists) — the yaml-shape view."""
        def plain(value):
            if isinstance(value, tuple):
                return [plain(v) for v in value]
            return value

        out = {"config_version": int(self.config_version),
               "pairs": plain(self.pairs)}
        for name in _SECTION_CLASSES:
            section = getattr(self, name)
            out[name] = {f.name: plain(getattr(section, f.name))
                         for f in fields(section)}
        return out

    def resolved_yaml_text(self) -> str:
        """Effective config (defaults + file + CLI overrides) as yaml text.

        This is what lands in store ``config_versions.yaml`` on supervisor
        start: the full truth of what the run used, not just the file on disk.
        """
        import yaml  # local import: loader-only dependency

        return yaml.safe_dump(self.to_dict(), sort_keys=False)




# -- per-gate veto flags (M0 / F-P1-4; M3 adds the regime/phase/fan-out gates) --
class VetoFlags(IntFlag):
    NONE = 0
    NO_VERDICT = 1
    MALFORMED = 2
    DAILY_LOSS_KILL = 4
    CAPITULATION_BLOCK = 8
    WHIPSAW = 16
    EXHAUSTION = 32
    LOW_CONFIDENCE = 64
    COOLDOWN = 128
    LOW_PUMP = 256
    LOW_DUMP = 512
    FUNDING_VETO = 1024
    WHIPSAW_FANOUT_TIE = 2048
    PHASE_NOT_IN_ENTRY_SET = 4096
    REGIME_CHOP = 8192
    REGIME_COUNTER = 16384
    PAIR_CAP = 32768            # M5: per-pair capacity exhausted (or dust)
    BASKET_CAP = 65536          # M5: side basket capacity exhausted (or dust)
    GLOBAL_DAILY_KILL = 131072  # M5: portfolio daily loss kill
    DRAWDOWN_HALT = 262144      # M5: portfolio drawdown halt


# gate name (as used in ``vetoed_by``) -> flag; order = canonical bit order.
GATE_FLAG = {
    "no_verdict": VetoFlags.NO_VERDICT,
    "malformed": VetoFlags.MALFORMED,
    "daily_loss_kill": VetoFlags.DAILY_LOSS_KILL,
    "capitulation": VetoFlags.CAPITULATION_BLOCK,
    "high_whipsaw": VetoFlags.WHIPSAW,
    "high_exhaustion": VetoFlags.EXHAUSTION,
    "low_confidence": VetoFlags.LOW_CONFIDENCE,
    "cooldown": VetoFlags.COOLDOWN,
    "low_pump": VetoFlags.LOW_PUMP,
    "low_dump": VetoFlags.LOW_DUMP,
    "funding": VetoFlags.FUNDING_VETO,
    "whipsaw_fanout_tie": VetoFlags.WHIPSAW_FANOUT_TIE,
    "phase_not_in_entry_set": VetoFlags.PHASE_NOT_IN_ENTRY_SET,
    "regime_chop": VetoFlags.REGIME_CHOP,
    "regime_counter": VetoFlags.REGIME_COUNTER,
    "pair_cap": VetoFlags.PAIR_CAP,
    "basket_cap": VetoFlags.BASKET_CAP,
    "global_daily_kill": VetoFlags.GLOBAL_DAILY_KILL,
    "drawdown_halt": VetoFlags.DRAWDOWN_HALT,
}
GATE_NAMES = tuple(GATE_FLAG)


def bitmask_for(names) -> int:
    """Combined bitmask for a list of gate names (unknown names ignored)."""
    mask = VetoFlags.NONE
    for name in names or []:
        mask |= GATE_FLAG.get(name, VetoFlags.NONE)
    return int(mask)


def bit_names(mask: int) -> list:
    """Gate names whose bit is set, in canonical bit order."""
    return [name for name, flag in GATE_FLAG.items() if int(mask) & int(flag)]


# -- decision ids (M0 / F-P1-3) --------------------------------------------
def new_decision_id() -> str:
    """Unique per-cycle id: UTC timestamp + random suffix (globally unique)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


# -- atomic JSON persistence (M0 / F-P2) -----------------------------------
def atomic_write_json(path, obj) -> None:
    """Write ``obj`` as JSON: tmp file in the same dir + os.replace.

    A crash before the replace leaves the previous file untouched and
    parseable; the tmp file is cleaned up on failure.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(target) + ".tmp")
    try:
        tmp.write_text(json.dumps(obj, indent=2))
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def append_jsonl(path, obj) -> None:
    """Append one JSON line. Logging must never break trading: swallow OSError."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, default=str) + "\n")
    except OSError:
        pass


# -- v2 config loader (M3) -------------------------------------------------
_SECTION_CLASSES = {
    "execution": ExecutionConfig,
    "regime": RegimeConfig,
    "fanout": FanoutConfig,
    "cache": CacheConfig,
    "burst": BurstConfig,
    "market": MarketConfig,
    "spot": RiskConfig,
    "perps": PerpsConfig,
    "portfolio": PortfolioConfig,
    "scout": ScoutConfig,
}

# Inclusive numeric bounds per field name (None = unbounded). Every numeric
# field of every section appears here; a violation is a startup ConfigError.
_RANGES = {
    "spot_fee_rate": (0.0, 1.0), "perps_taker_fee_rate": (0.0, 1.0),
    "taker_fee_rate": (0.0, 1.0), "slippage_rate": (0.0, 1.0),
    "max_position_fraction": (0.0, 1.0), "max_margin_fraction": (0.0, 1.0),
    "max_leverage": (0.0001, 125.0),
    "entry_min_pump": (0.0, 100.0), "short_min_dump": (0.0, 100.0),
    "entry_max_whipsaw": (0.0, 1.0), "short_max_whipsaw": (0.0, 1.0),
    "entry_max_exhaustion": (0.0, 1.0),
    "min_confidence": (0.0, 1.0),
    "exit_min_dump": (0.0, 100.0), "exit_hard_dump": (0.0, 100.0),
    "exit_min_pump": (0.0, 100.0), "exit_hard_pump": (0.0, 100.0),
    "exit_consecutive_cycles": (1, None), "min_hold_cycles": (1, None),
    "cooldown_seconds": (0, None), "daily_loss_limit_pct": (0.0, 100.0),
    "stop_loss_pct": (0.0, 100.0), "max_abs_funding_pct": (0.0, 100.0),
    "vol_window": (2, None), "atr_period": (1, None),
    "vol_ratio_threshold": (0.0, None),
    "donchian_period": (2, None), "donchian_history": (1, None),
    "narrow_percentile": (0.0, 1.0),
    "ema_period": (1, None), "ema_slope_bars": (1, None),
    "flat_slope": (0.0, 1.0),
    "band_low": (0.0, 1.0), "band_high": (0.0, 1.0),
    "cache_min_move": (0.0, 1.0), "cache_ttl": (0.0, None),
    "burst_threshold": (0.0, 1.0), "burst_trades": (0, None),
    "burst_cycles": (0, None),
    "ohlcv_ttl_seconds": (0.0, None), "ohlcv_15m_limit": (1, None),
    "ohlcv_1h_limit": (1, None),
    "pair_spot_cap": (0.0, 1.0), "pair_perps_margin_cap": (0.0, 1.0),
    "basket_long_cap": (0.0, 1.0), "basket_short_cap": (0.0, 1.0),
    "global_daily_loss": (-1.0, 1.0),
    "drawdown_halt": (0.0, 1.0), "drawdown_recover": (0.0, 1.0),
    "min_position_pct": (0.0, 1.0),
    "min_24h_vol_usd": (0.0, None), "min_listing_age_days": (0.0, None),
    "max_pairs": (1, None), "interval_seconds": (0.0, None),
    "gainers_per_page": (1, None),
}
_COUNTER_TREND = ("block", "allow")


def _warn(message: str) -> None:
    print(f"config warning: {message}", file=sys.stderr)


def _coerce_scalar(section: str, key: str, value, default):
    """Type-check + coerce one scalar to the dataclass field's type."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        raise ConfigError(
            f"config: {section}.{key}: expected a boolean, got {value!r}")
    if isinstance(value, bool):
        raise ConfigError(f"config: {section}.{key}: booleans are not valid values")
    if isinstance(default, int):
        if isinstance(value, int):
            return int(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise ConfigError(
            f"config: {section}.{key}: expected an integer, got {value!r}")
    if isinstance(default, float):
        if isinstance(value, (int, float)):
            return float(value)
        raise ConfigError(
            f"config: {section}.{key}: expected a number, got {value!r}")
    if isinstance(default, str):
        if isinstance(value, str):
            return value
        raise ConfigError(
            f"config: {section}.{key}: expected a string, got {value!r}")
    raise ConfigError(f"config: {section}.{key}: unsupported field type")


def _check_range(section: str, key: str, value) -> None:
    bounds = _RANGES.get(key)
    if bounds is None:
        return
    lo, hi = bounds
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ConfigError(
            f"config: {section}.{key}: {value!r} outside allowed range "
            f"[{lo}, {hi}]")


def _coerce_field(section: str, key: str, value, default):
    """One yaml value -> dataclass field value (tuple fields included)."""
    if key == "counter_trend":
        if value not in _COUNTER_TREND:
            raise ConfigError(
                f"config: {section}.{key}: expected one of {_COUNTER_TREND}, "
                f"got {value!r}")
        return value
    if isinstance(default, tuple):
        if not isinstance(value, (list, tuple)):
            raise ConfigError(
                f"config: {section}.{key}: expected a list, got {value!r}")
        if not default:  # no shape info: accept as tuple of raw values
            return tuple(value)
        elem_default = default[0]
        return tuple(_coerce_scalar(section, key, v, elem_default) for v in value)
    coerced = _coerce_scalar(section, key, value, default)
    if isinstance(coerced, (int, float)):
        _check_range(section, key, coerced)
    return coerced


def _validate_sections(cfg: V2Config) -> None:
    """Cross-field checks with clear startup errors."""
    if cfg.fanout.band_low >= cfg.fanout.band_high:
        raise ConfigError(
            f"config: fanout.band_low: {cfg.fanout.band_low!r} must be < "
            f"fanout.band_high {cfg.fanout.band_high!r}")
    for name in ("spot", "perps"):
        section = getattr(cfg, name)
        lo, hi = section.tier_thresholds
        if not lo < hi:
            raise ConfigError(
                f"config: {name}.tier_thresholds: must be strictly ascending, "
                f"got {section.tier_thresholds!r}")
        if len(section.tier_fractions) != 3:
            raise ConfigError(
                f"config: {name}.tier_fractions: expected 3 fractions, got "
                f"{list(section.tier_fractions)!r}")
        if any(not (0.0 < f <= 1.0) for f in section.tier_fractions):
            raise ConfigError(
                f"config: {name}.tier_fractions: fractions must be in (0, 1], "
                f"got {list(section.tier_fractions)!r}")
        for key in ("entry_phases", "short_entry_phases"):
            phases = getattr(section, key, None)
            if phases is None:  # short_entry_phases: perps only
                continue
            if not phases or any(not isinstance(p, str) for p in phases):
                raise ConfigError(
                    f"config: {name}.{key}: expected non-empty phase names, "
                    f"got {list(phases)!r}")
    # M5: pair universe + portfolio risk caps (strictly positive, halt > recover)
    if not cfg.pairs or any(not isinstance(p, str) or not p.strip()
                            for p in cfg.pairs):
        raise ConfigError(
            f"config: pairs: expected non-empty symbol names, "
            f"got {list(cfg.pairs)!r}")
    p = cfg.portfolio
    for key in ("pair_spot_cap", "pair_perps_margin_cap", "basket_long_cap",
                "basket_short_cap", "min_position_pct"):
        value = getattr(p, key)
        if not (0.0 < value <= 1.0):
            raise ConfigError(
                f"config: portfolio.{key}: {value!r} outside allowed range "
                f"(0, 1]")
    if not (-1.0 <= p.global_daily_loss < 0.0):
        raise ConfigError(
            f"config: portfolio.global_daily_loss: {p.global_daily_loss!r} "
            f"must be in [-1, 0)")
    if not (0.0 < p.drawdown_halt <= 1.0):
        raise ConfigError(
            f"config: portfolio.drawdown_halt: {p.drawdown_halt!r} "
            f"outside allowed range (0, 1]")
    if not (0.0 <= p.drawdown_recover < p.drawdown_halt):
        raise ConfigError(
            f"config: portfolio.drawdown_recover: {p.drawdown_recover!r} "
            f"must be in [0, drawdown_halt={p.drawdown_halt!r})")
    # M6: scout quote currency must be a real non-empty string
    quote = cfg.scout.quote
    if not isinstance(quote, str) or not quote.strip():
        raise ConfigError(
            f"config: scout.quote: expected a non-empty string, "
            f"got {quote!r}")


def load_config(path=None) -> V2Config:
    """config/v2.yaml overrides the frozen dataclass defaults.

    * omitted keys keep their dataclass default (single source of truth);
    * unknown keys / sections warn to stderr and are ignored (not fatal);
    * bad types, bad values and unsupported ``config_version`` raise
      ``ConfigError`` with a clear message at startup;
    * ``config_version`` must be 3 or 4 when present; absent -> 4 (v3 files
      load with the M6 scout defaults).
    """
    import yaml  # local import: PyYAML is the only non-stdlib dependency

    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        text = target.read_text()
    except OSError as exc:
        raise ConfigError(f"config file not readable: {target} ({exc})") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config: invalid yaml in {target}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"config: root of {target} must be a mapping, got {type(data).__name__}")

    version = data.get("config_version", CONFIG_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigError(f"config: config_version must be an integer, got {version!r}")
    if version not in SUPPORTED_CONFIG_VERSIONS:
        raise ConfigError(
            f"config: unsupported config_version {version} "
            f"(this loader supports {' or '.join(map(str, SUPPORTED_CONFIG_VERSIONS))})")

    overrides = {}
    for key, value in data.items():
        if key == "config_version":
            continue
        if key == "pairs":  # M5: root-level symbol list, not a mapping section
            if not isinstance(value, (list, tuple)):
                raise ConfigError(
                    f"config: pairs: expected a list of symbols, "
                    f"got {type(value).__name__}")
            overrides["pairs"] = tuple(value)
            continue
        if key not in _SECTION_CLASSES:
            _warn(f"unknown key {key!r} ignored")
            continue
        cls = _SECTION_CLASSES[key]
        defaults = cls()
        if not isinstance(value, dict):
            raise ConfigError(
                f"config: {key}: expected a mapping, got {type(value).__name__}")
        section_updates = {}
        for fkey, fvalue in value.items():
            if not any(f.name == fkey for f in fields(cls)):
                _warn(f"unknown key {key}.{fkey} ignored")
                continue
            section_updates[fkey] = _coerce_field(
                key, fkey, fvalue, getattr(defaults, fkey))
        overrides[key] = cls(**section_updates)

    cfg = V2Config(config_version=version, **overrides)
    _validate_sections(cfg)
    return cfg
