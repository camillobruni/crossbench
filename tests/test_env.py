# Copyright 2022 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import unittest
from unittest import mock

import crossbench as cb
import crossbench.runner
import crossbench.env


class HostEnvironmentTestCase(unittest.TestCase):

  def setUp(self):
    self.mock_platform = mock.Mock()
    self.mock_platform.processes.return_value = []
    self.mock_runner = mock.Mock(
        platform=self.mock_platform, probes=[], browsers=[])

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
    with mock.patch("logging.warn") as cm:
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
    with mock.patch("logging.warn") as cm:
      env.validate()
    cm.assert_not_called()
    self.mock_platform.sh_stdout.assert_not_called()
    self.mock_platform.sh.assert_not_called()

  def test_validate_warn_no_probes(self):
    env = cb.env.HostEnvironment(
        self.mock_runner, cb.env.HostEnvironmentConfig(require_probes=True),
        cb.env.ValidationMode.WARN)
    with mock.patch("logging.warn") as cm:
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
            browser_is_headless=cb.env.HostEnvironmentConfig.Ignore),
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

    mock_browser.is_headless = False
    with self.assertRaises(cb.env.ValidationError) as cm:
      env.validate()
    self.assertIn("is_headless", str(cm.exception))

    mock_browser.is_headless = True
    env.validate()

  def test_request_is_headless_false(self):
    env = cb.env.HostEnvironment(
        self.mock_runner,
        cb.env.HostEnvironmentConfig(browser_is_headless=False),
        cb.env.ValidationMode.THROW)
    mock_browser = mock.Mock()
    self.mock_runner.browsers = [mock_browser]

    mock_browser.is_headless = False
    env.validate()

    mock_browser.is_headless = True
    with self.assertRaises(cb.env.ValidationError) as cm:
      env.validate()
    self.assertIn("is_headless", str(cm.exception))