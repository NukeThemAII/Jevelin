#!/usr/bin/env python3
"""Tests for jev_config v2 (M3): YAML config loader + dataclass parity.

No network. Run: .venv/bin/python scripts/test_jev_config.py -v
"""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_config import (  # noqa: E402
    CONFIG_VERSION,
    ConfigError,
    ExecutionConfig,
    FanoutConfig,
    PerpsConfig,
    RegimeConfig,
    RiskConfig,
    V2Config,
    load_config,
)

SHIPPED_YAML = Path(__file__).resolve().parent.parent / "config" / "v2.yaml"


def _write_cfg(text):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    tmp.write(text)
    tmp.close()
    return tmp.name


class DefaultsParityTests(unittest.TestCase):
    def test_shipped_yaml_matches_dataclass_defaults(self):
        # Single source of truth: the shipped yaml values ARE the dataclass defaults.
        self.assertEqual(load_config(str(SHIPPED_YAML)), V2Config())

    def test_config_version_default_is_2(self):
        self.assertEqual(V2Config().config_version, 2)
        self.assertEqual(CONFIG_VERSION, 2)

    def test_shipped_yaml_exists(self):
        self.assertTrue(SHIPPED_YAML.exists())


class YamlOverrideTests(unittest.TestCase):
    def test_partial_yaml_overrides_only_given_keys(self):
        path = _write_cfg("config_version: 2\nspot:\n  entry_min_pump: 70.0\n")
        cfg = load_config(path)
        self.assertEqual(cfg.spot.entry_min_pump, 70.0)
        self.assertEqual(cfg.spot.min_confidence, RiskConfig().min_confidence)
        self.assertEqual(cfg.perps, PerpsConfig())
        self.assertEqual(cfg.execution, ExecutionConfig())
        self.assertEqual(cfg.regime, RegimeConfig())
        self.assertEqual(cfg.fanout, FanoutConfig())

    def test_yaml_list_coerces_to_tuple(self):
        path = _write_cfg("spot:\n  entry_phases: [breakout]\n"
                          "  tier_thresholds: [0.70, 0.85]\n"
                          "  tier_fractions: [0.6, 0.8, 1.0]\n")
        cfg = load_config(path)
        self.assertEqual(cfg.spot.entry_phases, ("breakout",))
        self.assertEqual(cfg.spot.tier_thresholds, (0.70, 0.85))
        self.assertEqual(cfg.spot.tier_fractions, (0.6, 0.8, 1.0))

    def test_int_accepted_for_float_fields(self):
        path = _write_cfg("perps:\n  max_leverage: 2\n")
        self.assertEqual(load_config(path).perps.max_leverage, 2.0)

    def test_all_sections_override(self):
        path = _write_cfg(
            "execution:\n  slippage_rate: 0.001\n"
            "regime:\n  flat_slope: 0.002\n"
            "fanout:\n  band_low: 0.3\n"
            "cache:\n  cache_ttl: 60.0\n"
            "burst:\n  burst_cycles: 5\n"
            "market:\n  ohlcv_1h_limit: 50\n"
            "spot:\n  cooldown_seconds: 60\n"
            "perps:\n  stop_loss_pct: 1.5\n")
        cfg = load_config(path)
        self.assertEqual(cfg.execution.slippage_rate, 0.001)
        self.assertEqual(cfg.regime.flat_slope, 0.002)
        self.assertEqual(cfg.fanout.band_low, 0.3)
        self.assertEqual(cfg.cache.cache_ttl, 60.0)
        self.assertEqual(cfg.burst.burst_cycles, 5)
        self.assertEqual(cfg.market.ohlcv_1h_limit, 50)
        self.assertEqual(cfg.spot.cooldown_seconds, 60)
        self.assertEqual(cfg.perps.stop_loss_pct, 1.5)


class UnknownKeyTests(unittest.TestCase):
    def test_unknown_key_warns_but_loads(self):
        path = _write_cfg("spot:\n  entry_min_pump: 70.0\n  entry_min_pumps: 99\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cfg = load_config(path)
        self.assertIn("unknown", err.getvalue())
        self.assertIn("entry_min_pumps", err.getvalue())
        self.assertEqual(cfg.spot.entry_min_pump, 70.0)  # not fatal

    def test_unknown_section_warns_but_loads(self):
        path = _write_cfg("telepathy:\n  enabled: true\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cfg = load_config(path)
        self.assertIn("telepathy", err.getvalue())
        self.assertEqual(cfg, V2Config())


class BadValueTests(unittest.TestCase):
    def assertConfigError(self, text, fragment):
        path = _write_cfg(text)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
        self.assertIn(fragment, str(ctx.exception))

    def test_bad_type_errors(self):
        self.assertConfigError("spot:\n  entry_min_pump: high\n", "entry_min_pump")
        self.assertConfigError("execution:\n  spot_fee_rate: '0.1'\n", "spot_fee_rate")
        self.assertConfigError("spot:\n  entry_phases: breakout\n", "entry_phases")

    def test_bad_value_errors(self):
        self.assertConfigError("execution:\n  spot_fee_rate: -0.1\n", "spot_fee_rate")
        self.assertConfigError("spot:\n  min_confidence: 1.5\n", "min_confidence")
        self.assertConfigError("spot:\n  counter_trend: sideways\n", "counter_trend")
        self.assertConfigError("spot:\n  exit_consecutive_cycles: 0\n", "exit_consecutive_cycles")
        self.assertConfigError("perps:\n  max_leverage: 0\n", "max_leverage")
        self.assertConfigError("fanout:\n  band_low: 0.9\nband_high: 0.1\n", "band_low")

    def test_tuple_shape_errors(self):
        self.assertConfigError("spot:\n  tier_thresholds: [0.9, 0.1]\n", "tier_thresholds")
        self.assertConfigError("spot:\n  tier_fractions: [0.6, 0.8]\n", "tier_fractions")

    def test_missing_file_errors(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config("/nonexistent/jevelin-config.yaml")
        self.assertIn("/nonexistent/jevelin-config.yaml", str(ctx.exception))

    def test_non_mapping_yaml_errors(self):
        self.assertConfigError("- just\n- a list\n", "mapping")


class ConfigVersionTests(unittest.TestCase):
    def test_omitted_defaults_to_2(self):
        self.assertEqual(load_config(_write_cfg("spot:\n  min_confidence: 0.7\n")).config_version, 2)

    def test_explicit_2_ok(self):
        self.assertEqual(load_config(_write_cfg("config_version: 2\n")).config_version, 2)

    def test_unsupported_version_errors(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(ConfigError) as ctx:
                load_config(_write_cfg("config_version: 3\n"))
        self.assertIn("config_version", str(ctx.exception))

    def test_non_int_version_errors(self):
        with self.assertRaises(ConfigError):
            load_config(_write_cfg("config_version: two\n"))


class ResolvedYamlTests(unittest.TestCase):
    def test_resolved_yaml_carrying_overrides(self):
        import yaml

        cfg = load_config(_write_cfg("spot:\n  entry_min_pump: 70.0\n"))
        text = cfg.resolved_yaml_text()
        back = yaml.safe_load(text)
        self.assertEqual(back["spot"]["entry_min_pump"], 70.0)
        self.assertEqual(back["config_version"], 2)
        self.assertEqual(back["perps"]["entry_min_pump"], 65.0)  # defaults carried


if __name__ == "__main__":
    unittest.main(verbosity=2)
