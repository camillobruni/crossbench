# Copyright 2022 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import csv
import sys
from unittest import mock

import pytest

from crossbench.benchmarks.motionmark import (MotionMark12Benchmark,
                                              MotionMark12Probe,
                                              MotionMark12Story)
from crossbench.env import HostEnvironmentConfig, ValidationMode
from crossbench.runner import Runner
from tests.benchmarks import helper


class MotionMark2Test(helper.PressBaseBenchmarkTestCase):

  @property
  def benchmark_cls(self):
    return MotionMark12Benchmark

  EXAMPLE_PROBE_DATA = [{
      "testsResults": {
          "MotionMark": {
              "Multiply": {
                  "complexity": {
                      "complexity": 1169.7666313745012,
                      "stdev": 2.6693101402239985,
                      "bootstrap": {
                          "confidenceLow": 1154.0859381321234,
                          "confidenceHigh": 1210.464520355893,
                          "median": 1180.8987652049277,
                          "mean": 1163.0061487765158,
                          "confidencePercentage": 0.8
                      },
                      "segment1": [[1, 16.666666666666668],
                                   [1, 16.666666666666668]],
                      "segment2": [[1, 6.728874992470971],
                                   [3105, 13.858528114770454]]
                  },
                  "controller": {
                      "score": 1168.106104032434,
                      "average": 1168.106104032434,
                      "stdev": 37.027504395081785,
                      "percent": 3.1698750881669624
                  },
                  "score": 1180.8987652049277,
                  "scoreLowerBound": 1154.0859381321234,
                  "scoreUpperBound": 1210.464520355893
              }
          }
      },
      "score": 1180.8987652049277,
      "scoreLowerBound": 1154.0859381321234,
      "scoreUpperBound": 1210.464520355893
  }]

  def test_all_stories(self):
    stories = self.story_filter(["all"], separate=True).stories
    self.assertGreater(len(stories), 1)
    for story in stories:
      self.assertIsInstance(story, MotionMark12Story)
    names = set(story.name for story in stories)
    self.assertEqual(len(names), len(stories))
    self.assertEqual(len(names), len(MotionMark12Story.SUBSTORIES))

  def test_default_stories(self):
    stories = self.story_filter(["default"], separate=True).stories
    self.assertGreater(len(stories), 1)
    for story in stories:
      self.assertIsInstance(story, MotionMark12Story)
    names = set(story.name for story in stories)
    self.assertEqual(len(names), len(stories))
    self.assertEqual(
        len(names), len(MotionMark12Story.ALL_STORIES["MotionMark"]))

  def test_run(self):
    stories = MotionMark12Story.from_names(['Multiply'])
    for browser in self.browsers:
      browser.js_side_effect = [
          True,  # Page is ready
          1,  # NOF enabled benchmarks
          None,  # Start running benchmark
          True,  # Wait until done
          self.EXAMPLE_PROBE_DATA
      ]
    repetitions = 3
    benchmark = self.benchmark_cls(stories)
    self.assertTrue(len(benchmark.describe()) > 0)
    runner = Runner(
        self.out_dir,
        self.browsers,
        benchmark,
        env_config=HostEnvironmentConfig(),
        env_validation_mode=ValidationMode.SKIP,
        platform=self.platform,
        repetitions=repetitions)
    with mock.patch.object(self.benchmark_cls, "validate_url") as cm:
      runner.run()
    cm.assert_called_once()
    for browser in self.browsers:
      urls = self.filter_data_urls(browser.url_list)
      self.assertEqual(len(urls), repetitions)
      self.assertIn(MotionMark12Probe.JS, browser.js_list)
    with (self.out_dir /
          f"{MotionMark12Probe.NAME}.csv").open(encoding="utf-8") as f:
      csv_data = list(csv.DictReader(f, delimiter="\t"))
    self.assertListEqual(list(csv_data[0].keys()), ["label", "dev", "stable"])
    self.assertDictEqual(csv_data[1], {
        'label': 'version',
        'dev': '102.22.33.44',
        'stable': '100.22.33.44'
    })


if __name__ == "__main__":
  sys.exit(pytest.main([__file__]))
