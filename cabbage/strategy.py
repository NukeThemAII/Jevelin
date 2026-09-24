"""Adapt the original examples/simple_app.py strategy; use the original pipeline.

This is RSI/EMA rule-based logic from upstream, not Jev inference. It uses only
closed bars. The original source and all original strategies remain unchanged.
"""
from datetime import datetime, timezone
import ccxt
import pandas as pd
from investing_algorithm_framework import (DataSource, DataType, PositionSize,
    StopLossRule, ExposureRule, Schedule, TimeUnit, SignalSide)
from examples.simple_app import RSIEMACrossoverStrategy

class CabbageStrategy(RSIEMACrossoverStrategy):
    strategy_id = 'cabbage-rsi-ema'

    def __init__(self, settings):
        self.settings = settings
        self.market = settings.market
        self.symbols = [settings.base]
        self.schedule = Schedule.every(settings.interval_seconds, TimeUnit.SECOND)
        self.data_sources = [DataSource(identifier='BTC_ohlcv',
            symbol=settings.symbol, data_type=DataType.OHLCV,
            time_frame=settings.timeframe, market=settings.market,
            pandas=True, warmup_window=100)]
        self.position_sizes = [PositionSize(symbol=settings.base, fixed_amount=settings.ticket)]
        self.stop_losses = [StopLossRule(symbol=settings.base,
            percentage_threshold=settings.stop_percent, sell_percentage=100,
            trailing=False)]
        self.take_profits = []
        self.scaling_rules = []
        self.cooldowns = []
        self.exposure_rule = ExposureRule(max_portfolio_percentage=100)
        # Spot-only configuration: preserve original long entry/exit rules.
        self.signal_cards = {side: card for side, card in RSIEMACrossoverStrategy.signal_cards.items()
            if side in (SignalSide.OPEN_LONG, SignalSide.CLOSE_LONG)}
        super().__init__(symbols=self.symbols, schedule=self.schedule,
            data_sources=self.data_sources, position_sizes=self.position_sizes,
            stop_losses=self.stop_losses, take_profits=[], scaling_rules=[],
            cooldowns=[], exposure_rule=self.exposure_rule, signal_cards=self.signal_cards)

    def prepare_signal_data(self, data):
        frame = data['BTC_ohlcv'].copy()
        if 'Datetime' in frame:
            timestamps = pd.to_datetime(frame['Datetime'], utc=True)
            seconds = ccxt.Exchange.parse_timeframe(self.settings.timeframe)
            cutoff = pd.Timestamp(datetime.now(timezone.utc)) - pd.Timedelta(seconds=seconds)
            frame = frame.loc[timestamps <= cutoff].copy()
        if len(frame) < 30:
            raise ValueError('Fewer than 30 closed candles; refusing to generate signals')
        # Reuse original RSI, EMA, crossover and confluence preparation unchanged.
        prepared = super().prepare_signal_data({'BTC_ohlcv':frame})
        return {self.settings.base:prepared['BTC']}
