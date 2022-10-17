# Copyright 2022 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from __future__ import annotations

import pathlib

import crossbench as cb
import crossbench.probes as probes


class V8BuiltinsPGOProbe(probes.Probe):
  """
  Chromium-only Probe to extract V8 builtins PGO data.
  The resulting data is used to optimize Torque and CSA builtins.
  """
  NAME = "v8.builtins.pgo"

  def is_compatible(self, browser: cb.browsers.Browser):
    return browser.type == "chrome"

  def attach(self, browser: cb.browsers.Browser):
    assert isinstance(browser, cb.browsers.Chrome)
    super().attach(browser)
    browser.js_flags.set("--allow-natives-syntax")

  class Scope(probes.Probe.Scope):

    def __init__(self, *args, **kwargs):
      super().__init__(*args, *kwargs)
      self._pgo_counters = None

    def setup(self, run: cb.runner.Run):
      pass

    def start(self, run: cb.runner.Run):
      pass

    def stop(self, run: cb.runner.Run):
      with run.actions("Extract Builtins PGO DATA") as actions:
        self._pgo_counters = actions.js(
            "return %GetAndResetTurboProfilingData();")

    def tear_down(self, run: cb.runner.Run):
      assert self._pgo_counters is not None and self._pgo_counters, (
          "Chrome didn't produce any V8 builtins PGO data. "
          "Please make sure to set the v8_enable_builtins_profiling=true "
          "gn args.")
      pgo_file = run.get_probe_results_file(self.probe)
      with pgo_file.open("a") as f:
        f.write(self._pgo_counters)
      return pgo_file

  def merge_repetitions(self, group: cb.runner.RepetitionsRunGroup):
    merged_result_path = group.get_probe_results_file(self)
    result_files = (pathlib.Path(run.results[self]) for run in group.runs)
    return self.runner_platform.concat_files(
        inputs=result_files, output=merged_result_path)

  def merge_stories(self, group: cb.runner.StoriesRunGroup):
    merged_result_path = group.get_probe_results_file(self)
    result_files = (
        pathlib.Path(group.results[self]) for group in group.repetitions_groups)
    return self.runner_platform.concat_files(
        inputs=result_files, output=merged_result_path)