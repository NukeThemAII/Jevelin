from dataclasses import dataclass
from math import isfinite
from pathlib import Path
import os
import ccxt

@dataclass(frozen=True)
class Settings:
    market: str = 'BITVAVO'
    symbol: str = 'BTC/EUR'
    timeframe: str = '2h'
    initial_balance: float = 1000
    ticket: float = 50
    stop_percent: float = 5
    fee_percent: float = 0.25
    interval_seconds: int = 7200
    state_root: Path = Path('runtime')

    @classmethod
    def from_env(cls):
        value = cls(
            market=os.getenv('CABBAGE_MARKET','BITVAVO').upper(),
            symbol=os.getenv('CABBAGE_SYMBOL','BTC/EUR').upper(),
            timeframe=os.getenv('CABBAGE_TIMEFRAME','2h'),
            initial_balance=float(os.getenv('CABBAGE_INITIAL_BALANCE','1000')),
            ticket=float(os.getenv('CABBAGE_TICKET','50')),
            stop_percent=float(os.getenv('CABBAGE_STOP_PERCENT','5')),
            fee_percent=float(os.getenv('CABBAGE_FEE_PERCENT','0.25')),
            interval_seconds=int(os.getenv('CABBAGE_INTERVAL_SECONDS','7200')),
            state_root=Path(os.getenv('CABBAGE_STATE_DIR','runtime')).resolve(),
        )
        value.validate()
        return value

    def validate(self):
        if self.market.lower() not in ccxt.exchanges:
            raise ValueError(f'{self.market} is not an available CCXT exchange. Robinhood Chain requires a separate adapter.')
        if len(self.symbol.split('/')) != 2 or ':' in self.symbol or not all(self.symbol.split('/')):
            raise ValueError('CABBAGE uses spot pairs BASE/QUOTE, e.g. BTC/EUR')
        numbers=(self.initial_balance,self.ticket,self.stop_percent,self.fee_percent)
        if not all(isfinite(x) for x in numbers): raise ValueError('Settings must be finite')
        if not 0 < self.ticket <= self.initial_balance: raise ValueError('Ticket must fit the initial balance')
        if not 0 < self.stop_percent < 100: raise ValueError('Stop must be between 0 and 100 percent')
        if not 0 <= self.fee_percent < 100: raise ValueError('Invalid fee')
        if self.interval_seconds < 1: raise ValueError('Interval must be positive')

    @property
    def base(self): return self.symbol.split('/')[0]
    @property
    def quote(self): return self.symbol.split('/')[1]
    def directory(self, mode):
        return self.state_root / mode / self.market.lower() / self.symbol.replace('/','-')
