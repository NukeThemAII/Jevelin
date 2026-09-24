import json
import os
from pathlib import Path
from investing_algorithm_framework import create_app, RESOURCE_DIRECTORY, PaperTradingMode
from .strategy import CabbageStrategy


def build_app(settings, mode):
    settings.validate()
    if mode not in ('paper','live'): raise ValueError('Mode must be paper or live')
    # Upstream supports environment overrides. Do not allow these to silently
    # convert an explicitly selected CABBAGE paper command to live trading.
    overrides=[name for name in os.environ if name.startswith(settings.market+'_OVERRIDE_')]
    if overrides:
        raise ValueError('Remove upstream market overrides for explicit CABBAGE modes: '+', '.join(overrides))
    if mode=='live' and not all(os.getenv(settings.market+s) for s in ('_API_KEY','_SECRET_KEY')):
        raise ValueError(f'Set {settings.market}_API_KEY and {settings.market}_SECRET_KEY in your local .env')
    folder=settings.directory(mode)
    folder.mkdir(parents=True,exist_ok=True)
    app=create_app(name='CABBAGE',config={RESOURCE_DIRECTORY:str(folder)})
    app.add_market(market=settings.market,trading_symbol=settings.quote,
        initial_balance=settings.initial_balance, fee_percentage=settings.fee_percent,
        paper_trading=(mode=='paper'), paper_trading_mode=PaperTradingMode.LOCAL)
    app.add_strategy(CabbageStrategy(settings))
    return app


def write_report(app, folder):
    report=app.get_last_run_report()
    if report is None: return
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    temp=folder/'latest-run.json.tmp'
    temp.write_text(json.dumps(report if isinstance(report,dict) else report.to_dict(),indent=2,default=str),encoding='utf-8')
    temp.replace(folder/'latest-run.json')
