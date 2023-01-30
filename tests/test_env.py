# Copyright 2022 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import pathlib
import sys
import unittest
from unittest import mock

import hjson
import pyfakefs.fake_filesystem_unittest
import pytest

import crossbench
import crossbench.env
import crossbench.runner

#TODO: fix imports
cb = crossbench


class HostEnvironmentConfigTestCase(unittest.TestCase):

  def test_combine_bool_value(self):
    default = cb.env.HostEnvironmentConfig()
    self.assertIsNone(default.power_use_battery)

    battery = cb.env.HostEnvironmentConfig(power_use_battery=True)
    self.assertTrue(battery.power_use_battery)
    self.assertTrue(battery.merge(battery).power_use_battery)
    self.assertTrue(default.merge(battery).power_use_battery)
    self.assertTrue(battery.merge(default).power_use_battery)

    power = cb.env.HostEnvironmentConfig(power_use_battery=False)
    self.assertFalse(power.power_use_battery)
    self.assertFalse(power.merge(power).power_use_battery)
    self.assertFalse(default.merge(power).power_use_battery)
    self.assertFalse(power.merge(default).power_use_battery)

    with self.assertRaises(ValueError):
      power.merge(battery)

  def test_combine_min_float_value(self):
    default = cb.env.HostEnvironmentConfig()
    self.assertIsNone(default.cpu_min_relative_speed)

    high = cb.env.HostEnvironmentConfig(cpu_min_relative_speed=1)
    self.assertEqual(high.cpu_min_relative_speed, 1)
    self.assertEqual(high.merge(high).cpu_min_relative_speed, 1)
    self.assertEqual(default.merge(high).cpu_min_relative_speed, 1)
    self.assertEqual(high.merge(default).cpu_min_relative_speed, 1)

    low = cb.env.HostEnvironmentConfig(cpu_min_relative_speed=0.5)
    self.assertEqual(low.cpu_min_relative_speed, 0.5)
    self.assertEqual(low.merge(low).cpu_min_relative_speed, 0.5)
    self.assertEqual(default.merge(low).cpu_min_relative_speed, 0.5)
    self.assertEqual(low.merge(default).cpu_min_relative_speed, 0.5)

    self.assertEqual(high.merge(low).cpu_min_relative_speed, 1)

  def test_combine_max_float_value(self):
    default = cb.env.HostEnvironmentConfig()
    self.assertIsNone(default.cpu_max_usage_percent)

    high = cb.env.HostEnvironmentConfig(cpu_max_usage_percent=100)
    self.assertEqual(high.cpu_max_usage_percent, 100)
    self.assertEqual(high.merge(high).cpu_max_usage_percent, 100)
    self.assertEqual(default.merge(high).cpu_max_usage_percent, 100)
    self.assertEqual(high.merge(default).cpu_max_usage_percent, 100)

    low = cb.env.HostEnvironmentConfig(cpu_max_usage_percent=0)
    self.assertEqual(low.cpu_max_usage_percent, 0)
    self.assertEqual(low.merge(low).cpu_max_usage_percent, 0)
    self.assertEqual(default.merge(low).cpu_max_usage_percent, 0)
    self.assertEqual(low.merge(default).cpu_max_usage_percent, 0)

    self.assertEqual(high.merge(low).cpu_max_usage_percent, 0)

  def test_parse_example_config_file(self):
    example_config_file = pathlib.Path(
        __file__).parent.parent / "config" / "env.config.example.hjson"
    if not example_config_file.exists():
      raise unittest.SkipTest(f"Test file {example_config_file} does not exist")
    with example_config_file.open(encoding="utf-8") as f:
      data = hjson.load(f)
    cb.env.HostEnvironmentConfig(**data["env"])


class HostEnvironmentTestCase(pyfakefs.fake_filesystem_unittest.TestCase):

  def setUp(self):
    self.setUpPyfakefs()
    self.mock_platform = mock.Mock()
    self.mock_platform.processes.return_value = []
    self.out_dir = pathlib.Path("results/current_benchmark_run_results")
    self.fs.create_file(self.out_dir)
    self.mock_runner = mock.Mock(
        platform=self.mock_platform,
        probes=[],
        browsers=[],
        out_dir=self.out_dir)

  def test_instantiate(self):
    env = cb.env.HostEnvironment(self.mock_runner)
    self.assertEqual(env.runner, self.mock_runner)

    config = cb.env.HostEnvironmentConfig()
    env = cb.env.HostEnvironment(self.mock_runner, config)
    self.assertEqual(env.runner, self.mock_runner)
    self.assertEqual(env.config, config)

  def test_warn_mode_skip(self):
    config = cb.env.HostEnvironmentConfig()
    env = cb.env.HostEnvironment(self.mock_runner, config,
                                 cb.env.ValidationMode.SKIP)
    env.handle_warning("foo")

  def test_warn_mode_fail(self):
    config = cb.env.HostEnvironmentConfig()
    env = cb.env.HostEnvironment(self.mock_runner, config,
                                 cb.env.ValidationMode.THROW)
    with self.assertRaises(cb.env.ValidationError) as cm:
      env.handle_warning("custom env check warning")
    self.assertIn("custom env check warning", str(cm.exception))

  def test_warn_mode_prompt(self):
    config = cb.env.HostEnvironmentConfig()
    env = cb.env.HostEnvironment(self.mock_runner, config,
                                 cb.env.ValidationMode.PROMPT)
    with mock.patch("builtins.input", return_value="Y") as cm:
      env.handle_warning("custom env check warning")
    cm.assert_called_once()
    self.assertIn("custom env check warning", cm.call_args[0][0])
    with mock.patch("builtins.input", return_value="n") as cm:
      with self.assertRaises(cb.env.ValidationError):
        env.handle_warning("custom env check warning")
    cm.assert_called_once()
    self.assertIn("custom env check warning", cm.call_args[0][0])

  def test_warn_mode_warn(self):
    config = cb.env.HostEnvironmentConfig()
    env = cb.env.HostEnvironment(self.mock_runner, config,
                                 cb.env.ValidationMode.WARN)
    with mock.patch("logging.warning") as cm:
      env.handle_warning("custom env check warning")
    cm.assert_called_once()
    self.assertIn("custom env check warning", cm.call_args[0][0])

  def test_validate_skip(self):
    env = cb.env.HostEnvironment(self.mock_runner,
                                 cb.env.HostEnvironmentConfig(),
                                 cb.env.ValidationMode.SKIP)
    env.validate()

  def test_validate_warn(self):
    env = cb.env.HostEnvironment(self.mock_runner,
                                 cb.env.HostEnvironmentConfig(),
                                 cb.env.ValidationMode.WARN)
    with mock.patch("logging.warning") as cm:
      env.validate()
    cm.assert_not_called()
    self.mock_platform.sh_stdout.assert_not_called()
    self.mock_platform.sh.assert_not_called()

  def test_validate_warn_no_probes(self):
    env = cb.env.HostEnvironment(
        self.mock_runner, cb.env.HostEnvironmentConfig(require_probes=True),
        cb.env.ValidationMode.WARN)
    with mock.patch("logging.warning") as cm:
      env.validate()
    cm.assert_called_once()
    self.mock_platform.sh_stdout.assert_not_called()
    self.mock_platform.sh.assert_not_called()

  def test_request_battery_power_on(self):
    env = cb.env.HostEnvironment(
        self.mock_runner, cb.env.HostEnvironmentConfig(power_use_battery=True),
        cb.env.ValidationMode.THROW)
    self.mock_platform.is_battery_powered = True
    env.validate()

    self.mock_platform.is_battery_powered = False
    with self.assertRaises(Exception) as cm:
      env.validate()
    self.assertIn("battery", str(cm.exception).lower())

  def test_request_battery_power_off(self):
    env = cb.env.HostEnvironment(
        self.mock_runner, cb.env.HostEnvironmentConfig(power_use_battery=False),
        cb.env.ValidationMode.THROW)
    self.mock_platform.is_battery_powered = True
    with self.assertRaises(cb.env.ValidationError) as cm:
      env.validate()
    self.assertIn("battery", str(cm.exception).lower())

    self.mock_platform.is_battery_powered = False
    env.validate()

  def test_request_battery_power_off_conflicting_probe(self):
    env = cb.env.HostEnvironment(
        self.mock_runner, cb.env.HostEnvironmentConfig(power_use_battery=False),
        cb.env.ValidationMode.THROW)
    self.mock_platform.is_battery_powered = False

    mock_probe = mock.Mock()
    mock_probe.configure_mock(BATTERY_ONLY=True, name="mock_probe")
    self.mock_runner.probes = [mock_probe]

    with self.assertRaises(cb.env.ValidationError) as cm:
      env.validate()
    message = str(cm.exception).lower()
    self.assertIn("mock_probe", message)
    self.assertIn("battery", message)

    mock_probe.BATTERY_ONLY = False
    env.validate()

  def test_request_is_headless_default(self):
    env = cb.env.HostEnvironment(
        self.mock_runner,
        cb.env.HostEnvironmentConfig(
            browser_is_headless=cb.env.HostEnvironmentConfig.IGNORE),
        cb.env.ValidationMode.THROW)
    mock_browser = mock.Mock()
    self.mock_runner.browsers = [mock_browser]

    mock_browser.is_headless = False
    env.validate()

    mock_browser.is_headless = True
    env.validate()

  def test_request_is_headless_true(self):
    env = cb.env.HostEnvironment(
        self.mock_runner,
        cb.env.HostEnvironmentConfig(browser_is_headless=True),
        cb.env.ValidationMode.THROW)
    mock_browser = mock.Mock()
    self.mock_runner.browsers = [mock_browser]

    self.mock_platform.has_display = True
    mock_browser.is_headless = False
    with self.assertRaises(cb.env.ValidationError) as cm:
      env.validate()
    self.assertIn("is_headless", str(cm.exception))

    self.mock_platform.has_display = False
    with self.assertRaises(cb.env.ValidationError) as cm:
      env.validate()

    self.mock_platform.has_display = True
    mock_browser.is_headless = True
    env.validate()

    self.mock_platform.has_display = False
    env.validate()

  def test_request_is_headless_false(self):
    env = cb.env.HostEnvironment(
        self.mock_runner,
        cb.env.HostEnvironmentConfig(browser_is_headless=False),
        cb.env.ValidationMode.THROW)
    mock_browser = mock.Mock()
    self.mock_runner.browsers = [mock_browser]

    self.mock_platform.has_display = True
    mock_browser.is_headless = False
    env.validate()

    self.mock_platform.has_display = False
    with self.assertRaises(cb.env.ValidationError) as cm:
      env.validate()

    self.mock_platform.has_display = True
    mock_browser.is_headless = True
    with self.assertRaises(cb.env.ValidationError) as cm:
      env.validate()
    self.assertIn("is_headless", str(cm.exception))

  def test_results_dir_single(self):
    env = cb.env.HostEnvironment(self.mock_runner)
    with mock.patch("logging.warning") as cm:
      env.validate()
    cm.assert_not_called()

  def test_results_dir_non_existent(self):
    self.mock_runner.out_dir = pathlib.Path("does/not/exist")
    env = cb.env.HostEnvironment(self.mock_runner)
    with mock.patch("logging.warning") as cm:
      env.validate()
    cm.assert_not_called()

  def test_results_dir_many(self):
    # Create fake test result dirs:
    for i in range(30):
      (self.out_dir.parent / str(i)).mkdir()
    env = cb.env.HostEnvironment(self.mock_runner)
    with mock.patch("logging.warning") as cm:
      env.validate()
    cm.assert_called_once()

  def test_results_dir_too_many(self):
    # Create fake test result dirs:
    for i in range(100):
      (self.out_dir.parent / str(i)).mkdir()
    env = cb.env.HostEnvironment(self.mock_runner)
    with mock.patch("logging.error") as cm:
      env.validate()
    cm.assert_called_once()

  def test_check_installed_missing(self):

    def which_none(_):
      return None

    self.mock_platform.which = which_none
    env = cb.env.HostEnvironment(self.mock_runner)
    with self.assertRaises(cb.env.ValidationError) as cm:
      env.check_installed(["custom_binary"])
    self.assertIn("custom_binary", str(cm.exception))
    with self.assertRaises(cb.env.ValidationError) as cm:
      env.check_installed(["custom_binary_a", "custom_binary_b"])
    self.assertIn("custom_binary_a", str(cm.exception))
    self.assertIn("custom_binary_b", str(cm.exception))

  def test_check_installed_partially_missing(self):

    def which_custom(binary):
      if binary == "custom_binary_b":
        return "/bin/custom_binary_b"
      return None

    self.mock_platform.which = which_custom
    env = cb.env.HostEnvironment(self.mock_runner)
    env.check_installed(["custom_binary_b"])
    with self.assertRaises(cb.env.ValidationError) as cm:
      env.check_installed(["custom_binary_a", "custom_binary_b"])
    self.assertIn("custom_binary_a", str(cm.exception))
    self.assertNotIn("custom_binary_b", str(cm.exception))


if __name__ == "__main__":
  sys.exit(pytest.main([__file__]))
