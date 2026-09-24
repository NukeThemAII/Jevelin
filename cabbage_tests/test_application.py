import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import polars as pl
from investing_algorithm_framework import Signal,SignalSide,CSVOHLCVDataProvider
from investing_algorithm_framework.infrastructure.database import teardown_sqlalchemy
from cabbage.config import Settings
from cabbage.application import build_app,write_report
from cabbage.strategy import CabbageStrategy

class CabbageApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(teardown_sqlalchemy)
        self.s=Settings(state_root=Path(self.tmp.name))

    def test_upstream_confluence_buy_and_sell(self):
        strategy=CabbageStrategy(self.s)
        frame=pd.DataFrame({'rsi':[20.,20.], 'recent_crossover':[1.,1.],
                            'recent_crossunder':[0.,0.]})
        with patch.object(strategy,'prepare_signal_data',return_value={'BTC':frame}):
            signals=list(strategy.generate_signals(None,{}))
        self.assertEqual([s.side for s in signals],[SignalSide.OPEN_LONG])
        frame['rsi']=80.;frame['recent_crossover']=0.;frame['recent_crossunder']=1.
        with patch.object(strategy,'prepare_signal_data',return_value={'BTC':frame}):
            signals=list(strategy.generate_signals(None,{}))
        self.assertEqual([s.side for s in signals],[SignalSide.CLOSE_LONG])

    def test_live_wires_original_ccxt_executor_without_sending_orders(self):
        from investing_algorithm_framework.infrastructure import CCXTOrderExecutor,PaperTradingOrderExecutor
        with patch.dict(os.environ,{'BITVAVO_API_KEY':'test-key','BITVAVO_SECRET_KEY':'test-secret'},clear=True):
            app=build_app(self.s,'live')
        app.initialize_config()
        app.initialize_order_executors()
        executors=app.container.order_executor_lookup().get_all()
        self.assertTrue(any(isinstance(e,CCXTOrderExecutor) for e in executors))
        self.assertFalse(any(isinstance(e,PaperTradingOrderExecutor) for e in executors))

    def test_configured_pair_and_risk_reach_framework(self):
        s=Settings(market='BINANCE',symbol='SOL/USDT',ticket=75,stop_percent=7,state_root=Path(self.tmp.name))
        strategy=CabbageStrategy(s)
        self.assertEqual(strategy.symbols,['SOL'])
        self.assertEqual(strategy.data_sources[0].symbol,'SOL/USDT')
        self.assertEqual(strategy.position_sizes[0].fixed_amount,75)
        self.assertEqual(strategy.stop_losses[0].percentage_threshold,7)
        self.assertFalse(strategy.stop_losses[0].trailing)
        self.assertNotIn(SignalSide.OPEN_SHORT,strategy.signal_cards)

    def test_paper_cannot_be_overridden_to_live(self):
        with patch.dict(os.environ,{'BITVAVO_OVERRIDE_PAPER_TRADING':'false'}):
            with self.assertRaises(ValueError): build_app(self.s,'paper')

    def test_live_requires_keys(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(ValueError):build_app(self.s,'live')

    def test_full_framework_buy_fill_sell_fill(self):
        """Synthetic market quotes, real framework services and SQLite accounting."""
        app=build_app(self.s,'paper')
        fixture=Path(__file__).resolve().parents[1]/'tests/resources/data/OHLCV_BTC-EUR_BITVAVO_2h_2024-06-01-00-00_2024-06-21-00-00.csv'
        app.add_data_provider(CSVOHLCVDataProvider(storage_path=str(fixture),symbol='BTC/EUR',time_frame='2h',market='BITVAVO',warmup_window=100),priority=1)
        base='investing_algorithm_framework.services.data_providers.DataProviderService.'
        with patch.object(CSVOHLCVDataProvider,'has_data',return_value=True), \
             patch.object(CSVOHLCVDataProvider,'get_data',return_value=pl.read_csv(fixture,try_parse_dates=True)), \
             patch(base+'get_ticker_data',return_value={'symbol':'BTC/EUR','ask':100.,'bid':100.}), \
             patch(base+'get_ohlcv_data') as bars, \
             patch.object(CabbageStrategy,'generate_signals') as signals:
            bars.return_value=None
            signals.return_value=iter([Signal(symbol='BTC',side=SignalSide.OPEN_LONG)])
            app.run(number_of_iterations=1)
            orders=app.container.order_service().get_all()
            self.assertEqual(len(orders),1)
            self.assertEqual(orders[0].status,'OPEN')
            self.assertAlmostEqual(float(orders[0].amount)*float(orders[0].price),50.,places=1)
            def next_bar(order,price):
                return pl.DataFrame({'Datetime':[order.updated_at+timedelta(seconds=1)],
                    'Open':[price],'High':[price+2.],'Low':[price-2.],
                    'Close':[price],'Volume':[1000.]})
            bars.return_value=next_bar(orders[0],100.)
            signals.return_value=iter([])
            app.run(number_of_iterations=1)
            self.assertEqual(app.container.order_service().get_all()[0].status,'CLOSED')
            signals.return_value=iter([Signal(symbol='BTC',side=SignalSide.CLOSE_LONG)])
            app.run(number_of_iterations=1)
            orders=app.container.order_service().get_all()
            sell=[o for o in orders if str(o.order_side).upper()=='SELL']
            self.assertEqual(len(sell),1)
            bars.return_value=next_bar(sell[0],100.)
            signals.return_value=iter([])
            app.run(number_of_iterations=1)
            orders=app.container.order_service().get_all()
            self.assertTrue(all(o.status=='CLOSED' for o in orders))
            self.assertTrue(all(o.external_id.startswith('paper-') for o in orders))
            report=app.get_last_run_report()
            self.assertTrue(report['is_paper'])
            self.assertTrue(report['trades'])
            write_report(app,self.s.directory('paper'))
            self.assertTrue((self.s.directory('paper')/'latest-run.json').exists())

if __name__=='__main__':unittest.main()
