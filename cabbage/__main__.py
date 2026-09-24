import argparse
import json
import os
from pathlib import Path
import sys
from dotenv import load_dotenv
from .config import Settings

ROOT=Path(__file__).resolve().parents[1]

def doctor(settings, online=False):
    import ccxt
    from .strategy import CabbageStrategy
    strategy=CabbageStrategy(settings)
    result={'framework':'9.0.0a18','strategy':strategy.strategy_id,
        'market':settings.market,'symbol':settings.symbol,
        'ticket_quote_currency':settings.ticket,'stop_percent':settings.stop_percent,
        'credentials_configured':all(os.getenv(settings.market+s) for s in ('_API_KEY','_SECRET_KEY')),
        'Jev':'not present in upstream','Robinhood Chain':'not present in upstream'}
    if online:
        exchange=getattr(ccxt,settings.market.lower())({'enableRateLimit':True,'timeout':15000})
        markets=exchange.load_markets()
        if settings.symbol not in markets: raise ValueError('Pair unavailable on selected exchange')
        ticker=exchange.fetch_ticker(settings.symbol)
        result['public_ticker']={k:ticker.get(k) for k in ('symbol','timestamp','bid','ask','last')}
    print(json.dumps(result,indent=2))

def backtest():
    from datetime import datetime, timezone
    from investing_algorithm_framework import (create_app, RESOURCE_DIRECTORY,
        CSVOHLCVDataProvider, Study, Universe, BacktestEngine, BacktestWindow,
        BacktestDateRange, BacktestRunConfiguration, BacktestReport)
    from .strategy import CabbageStrategy
    # Original upstream historical fixture; an actual framework event backtest.
    settings=Settings(state_root=ROOT/'runtime', interval_seconds=7200)
    folder=settings.directory('backtest');folder.mkdir(parents=True,exist_ok=True)
    app=create_app(name='CABBAGE-backtest',config={RESOURCE_DIRECTORY:str(folder)})
    app.add_market(market=settings.market,trading_symbol=settings.quote,
        initial_balance=settings.initial_balance,fee_percentage=settings.fee_percent)
    fixture=ROOT/'tests/resources/data/OHLCV_BTC-EUR_BITVAVO_2h_2024-06-01-00-00_2024-06-21-00-00.csv'
    app.add_data_provider(CSVOHLCVDataProvider(storage_path=str(fixture),
        symbol=settings.symbol,time_frame=settings.timeframe,market=settings.market,
        warmup_window=100),priority=1)
    study=Study(name='cabbage-historical-check',
        universe=Universe(market=settings.market,trading_symbol=settings.quote,symbols=[settings.base]),
        initial_capital=settings.initial_balance,risk_free_rate=0,
        engines=[BacktestEngine.EVENT_DRIVEN],backtest_windows=[BacktestWindow(
            train_range=BacktestDateRange(start_date=datetime(2024,6,10,tzinfo=timezone.utc),
                end_date=datetime(2024,6,20,tzinfo=timezone.utc)))])
    results=app.run_backtest(strategy=CabbageStrategy(settings),study=study,
        run_configuration=BacktestRunConfiguration(backtest_storage_directory=str(folder/'results'),
            show_progress=False,continue_on_error=False,n_workers=1))
    backtests=results.load_backtests(workers=1)
    BacktestReport(backtests=backtests).save(str(folder/'report.html'))
    columns=[c for c in ('algorithm_id','engine_type','summary.number_of_trades','summary.total_net_gain') if c in results.df]
    print(results.df[columns].to_string(index=False))
    print('Report:',folder/'report.html')

def main():
    load_dotenv(ROOT/'.env')
    parser=argparse.ArgumentParser(description='CABBAGE — original Investing Algorithm Framework runtime')
    parser.add_argument('command',choices=['doctor','paper','live','backtest'])
    parser.add_argument('--online',action='store_true',help='Check public exchange data without sending orders')
    parser.add_argument('--iterations',type=int,default=None,help='Stop after this many framework iterations')
    args=parser.parse_args()
    if args.iterations is not None and args.iterations < 1: parser.error('iterations must be positive')
    settings=Settings.from_env()
    if args.command=='doctor': return doctor(settings,args.online)
    if args.command=='backtest': return backtest()
    from .application import build_app,write_report
    app=build_app(settings,args.command)
    print(f'CABBAGE {args.command.upper()} / {settings.market} / {settings.symbol}',flush=True)
    print(f'Fixed ticket {settings.ticket} {settings.quote}; stop {settings.stop_percent}%',flush=True)
    try:
        app.run(number_of_iterations=args.iterations,run_immediately_on_start=True)
    finally:
        write_report(app,settings.directory(args.command))

if __name__=='__main__':
    try: main()
    except KeyboardInterrupt: print('Stopped. Framework state remains on disk.')
    except Exception as exc:
        print(f'CABBAGE error: {exc}',file=sys.stderr)
        raise SystemExit(1)
