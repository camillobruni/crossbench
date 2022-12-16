# Copyright 2022 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from __future__ import annotations

import abc
import csv
import logging
from collections import defaultdict
import pathlib
from typing import TYPE_CHECKING, Any, Dict, Final, Tuple, Type

from tabulate import tabulate

from crossbench.benchmarks.base import PressBenchmark
from crossbench.probes import helper as probes_helper
from crossbench.probes.json import JsonResultProbe
from crossbench.stories import PressBenchmarkStory

if TYPE_CHECKING:
  from crossbench.runner import BrowsersRunGroup, Runner, StoriesRunGroup, Run


class JetStream2Probe(JsonResultProbe, metaclass=abc.ABCMeta):
  """
  JetStream2-specific Probe.
  Extracts all JetStream2 times and scores.
  """
  IS_GENERAL_PURPOSE: Final[bool] = False
  FLATTEN: Final[bool] = False
  JS: Final[str] = """
  let results = Object.create(null);
  for (let benchmark of JetStream.benchmarks) {
    const data = { score: benchmark.score };
    if ("worst4" in benchmark) {
      data.firstIteration = benchmark.firstIteration;
      data.average = benchmark.average;
      data.worst4 = benchmark.worst4;
    } else if ("runTime" in benchmark) {
      data.runTime = benchmark.runTime;
      data.startupTime = benchmark.startupTime;
    }
    results[benchmark.plan.name] = data;
  };
  return results;
"""

  def to_json(self, actions):
    data = actions.js(self.JS)
    assert len(data) > 0, "No benchmark data generated"
    return data

  def process_json_data(self, json_data):
    assert "Total" not in json_data
    json_data["Total"] = self._compute_total_metrics(json_data)
    return json_data

  def _compute_total_metrics(self,
                             json_data: Dict[str, Any]) -> Dict[str, float]:
    # Manually add all total scores
    accumulated_metrics = defaultdict(list)
    for _, metrics in json_data.items():
      for metric, value in metrics.items():
        accumulated_metrics[metric].append(value)
    total: Dict[str, float] = {}
    for metric, values in accumulated_metrics.items():
      total[metric] = probes_helper.geomean(values)
    return total

  def merge_stories(self, group: StoriesRunGroup):
    merged = probes_helper.ValuesMerger.merge_json_list(
        story_group.results[self].json
        for story_group in group.repetitions_groups)
    return self.write_group_result(group, merged, write_csv=True)

  def merge_browsers(self, group: BrowsersRunGroup):
    return self.merge_browsers_json_list(group).merge(
        self.merge_browsers_csv_list(group))

  def log_result_summary(self, runner: Runner):
    if self not in runner.browser_group.results:
      return
    results_csv: pathlib.Path = runner.browser_group.results[self].csv
    with results_csv.open(encoding="utf-8") as f:
      # TODO: add merged JSON to read data in a more structured way
      data = list(csv.reader(f, delimiter="\t"))
      table = data[:3] + data[5:10]
      logging.info("-" * 80)
      logging.info("JetStream results:")
      logging.info("  %s", results_csv.relative_to(pathlib.Path.cwd()))
      logging.info("." * 80)
      logging.info(tabulate(table, tablefmt="plain"))


class JetStream20Probe(JetStream2Probe):
  __doc__ = JetStream2Probe.__doc__
  NAME: str = "jetstream_2.0"


class JetStream21Probe(JetStream2Probe):
  __doc__ = JetStream2Probe.__doc__
  NAME: str = "jetstream_2.1"


class JetStream2Story(PressBenchmarkStory, metaclass=abc.ABCMeta):
  URL_LOCAL: Final[str] = "http://localhost:8000/"
  SUBSTORIES: Final[Tuple[str, ...]] = (
      "WSL",
      "UniPoker",
      "uglify-js-wtb",
      "typescript",
      "tsf-wasm",
      "tagcloud-SP",
      "string-unpack-code-SP",
      "stanford-crypto-sha256",
      "stanford-crypto-pbkdf2",
      "stanford-crypto-aes",
      "splay",
      "segmentation",
      "richards-wasm",
      "richards",
      "regexp",
      "regex-dna-SP",
      "raytrace",
      "quicksort-wasm",
      "prepack-wtb",
      "pdfjs",
      "OfflineAssembler",
      "octane-zlib",
      "octane-code-load",
      "navier-stokes",
      "n-body-SP",
      "multi-inspector-code-load",
      "ML",
      "mandreel",
      "lebab-wtb",
      "json-stringify-inspector",
      "json-parse-inspector",
      "jshint-wtb",
      "HashSet-wasm",
      "hash-map",
      "gcc-loops-wasm",
      "gbemu",
      "gaussian-blur",
      "float-mm.c",
      "FlightPlanner",
      "first-inspector-code-load",
      "espree-wtb",
      "earley-boyer",
      "delta-blue",
      "date-format-xparb-SP",
      "date-format-tofte-SP",
      "crypto-sha1-SP",
      "crypto-md5-SP",
      "crypto-aes-SP",
      "crypto",
      "coffeescript-wtb",
      "chai-wtb",
      "cdjs",
      "Box2D",
      "bomb-workers",
      "Basic",
      "base64-SP",
      "babylon-wtb",
      "Babylon",
      "async-fs",
      "Air",
      "ai-astar",
      "acorn-wtb",
      "3d-raytrace-SP",
      "3d-cube-SP",
  )

  @property
  def substory_duration(self) -> float:
    return 2

  def run(self, run: Run):
    with run.actions("Setup") as actions:
      actions.navigate_to(self._url)
      if self._substories != self.SUBSTORIES:
        actions.wait_js_condition(("return JetStream && JetStream.benchmarks "
                                   "&& JetStream.benchmarks.length > 0;"), 0.1,
                                  10)
        actions.js(
            """
        let benchmarks = arguments[0];
        JetStream.benchmarks = JetStream.benchmarks.filter(
            benchmark => benchmarks.includes(benchmark.name));
        """,
            arguments=[self._substories])
      actions.wait_js_condition(
          """
        return document.querySelectorAll("#results>.benchmark").length > 0;
      """, 1, 30 + self.duration)
    with run.actions("Running") as actions:
      actions.js("JetStream.start()")
      actions.wait(self.fast_duration)
    with run.actions("Waiting for completion") as actions:
      actions.wait_js_condition(
          """
        let summaryElement = document.getElementById("result-summary");
        return (summaryElement.classList.contains("done"));
        """, self.substory_duration, self.slow_duration)


ProbeClsTupleT = Tuple[Type[JetStream2Probe], ...]


class JetStream20Story(JetStream2Story):
  __doc__ = JetStream2Story.__doc__
  NAME: Final[str] = "jetstream_2.0"
  URL: Final[str] = "https://browserbench.org/JetStream2.0/"
  PROBES: Final[ProbeClsTupleT] = (JetStream20Probe,)


class JetStream21Story(JetStream2Story):
  __doc__ = JetStream2Story.__doc__
  NAME: Final[str] = "jetstream_2.1"
  URL: Final[str] = "https://browserbench.org/JetStream2.1/"
  PROBES: Final[ProbeClsTupleT] = (JetStream21Probe,)


class JetStream2Benchmark(PressBenchmark, metaclass=abc.ABCMeta):
  pass


class JetStream20Benchmark(JetStream2Benchmark):
  """
  Benchmark runner for JetStream 2.0.
  """

  NAME: Final[str] = "jetstream_2.0"
  DEFAULT_STORY_CLS = JetStream20Story


class JetStream21Benchmark(JetStream2Benchmark):
  """
  Benchmark runner for JetStream 2.1.
  """

  NAME: Final[str] = "jetstream_2.1"
  DEFAULT_STORY_CLS = JetStream21Story
