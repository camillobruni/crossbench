# Copyright 2022 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from __future__ import annotations

import abc
import argparse
from collections.abc import Callable, Iterable, Mapping
import contextlib
import dataclasses
import datetime as dt
import inspect
import json
import logging
import pathlib
import sys
import threading
from typing import (TYPE_CHECKING, Any, Dict, Iterable, Iterator, List,
                    Optional, Sequence, Tuple, Type, Union)

from crossbench import cli_helper, exception, helper
from crossbench.env import (HostEnvironment, HostEnvironmentConfig,
                            ValidationMode)
from crossbench.flags import Flags, JSFlags
from crossbench.platform import DEFAULT_PLATFORM, Platform
from crossbench.probes.probe import Probe, ProbeScope, ResultLocation
from crossbench.probes.results import (EmptyProbeResult, ProbeResult,
                                       ProbeResultDict)
from crossbench.probes.runner import (RunDurationsProbe, RunResultsSummaryProbe,
                                      RunRunnerLogProbe)

if TYPE_CHECKING:
  from crossbench.benchmarks.benchmark import Benchmark
  from crossbench.browsers.browser import Browser
  from crossbench.stories import Story


class RunnerException(exception.MultiException):
  pass


@dataclasses.dataclass(frozen=True)
class Timing:
  cool_down_time: dt.timedelta = dt.timedelta(seconds=1)
  unit: dt.timedelta = dt.timedelta(seconds=1)

  def units(self, time: Union[float, int, dt.timedelta]) -> float:
    if isinstance(time, dt.timedelta):
      seconds = time.total_seconds()
    else:
      seconds = time
    assert seconds > 0, f"Unexpected negative time: {seconds}s"
    return seconds / self.unit.total_seconds()

  def timedelta(self,
                time_unit: Union[float, int, dt.timedelta],
                absolute: bool = False) -> dt.timedelta:
    if absolute:
      if isinstance(time_unit, dt.timedelta):
        return time_unit
      return dt.timedelta(seconds=time_unit)
    assert isinstance(time_unit, (float, int))
    assert time_unit >= 0
    return time_unit * self.unit


class ThreadMode(helper.StrEnumWithHelp):
  NONE = ("none", (
      "Execute all Runs sequentially, default. "
      "Low interference risk, use for worry-free time-critical measurements."))
  PLATFORM = ("platform",
              ("Execute runs from each platform in parallel threads. "
               "Might cause some interference with probes that do heavy "
               "post-processing."))
  BROWSER = ("browser", (
      "Execute runs from each browser in parallel thread. "
      "High interference risk, don't use for time-critical measurements."))
  RUN = ("run",
         ("Execute each Run in a parallel thread. "
          "High interference risk, don't use for time-critical measurements."))

  def group(self, runs: List[Run]) -> List[RunThreadGroup]:
    if self == ThreadMode.NONE:
      return [RunThreadGroup(runs)]
    if self == ThreadMode.RUN:
      return [RunThreadGroup([run]) for run in runs]
    groups: Dict[Any, List[Run]] = {}
    if self == ThreadMode.PLATFORM:
      groups = helper.group_by(runs, lambda run: run.platform)
    elif self == ThreadMode.BROWSER:
      groups = helper.group_by(runs, lambda run: run.browser)
    else:
      raise ValueError(f"Unexpected thread mode: {self}")
    return [RunThreadGroup(runs) for runs in groups.values()]


class Runner:

  @classmethod
  def get_out_dir(cls, cwd: pathlib.Path, suffix: str = "",
                  test: bool = False) -> pathlib.Path:
    if test:
      return cwd / "results" / "test"
    if suffix:
      suffix = "_" + suffix
    return (cwd / "results" /
            f"{dt.datetime.now().strftime('%Y-%m-%d_%H%M%S')}{suffix}")

  @classmethod
  def add_cli_parser(
      cls, benchmark_cls: Type[Benchmark],
      parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "--repeat",
        "-r",
        default=1,
        type=cli_helper.parse_positive_int,
        help="Number of times each benchmark story is "
        "repeated. Defaults to 1")

    parser.add_argument(
        "--parallel",
        default=ThreadMode.NONE,
        type=ThreadMode,
        help=("Change how Runs are executed.\n" +
              ThreadMode.help_text(indent=2)))

    out_dir_group = parser.add_argument_group("Output Directory Options")
    out_dir_xor_group = out_dir_group.add_mutually_exclusive_group()
    out_dir_xor_group.add_argument(
        "--out-dir",
        "--output-directory",
        "-o",
        type=pathlib.Path,
        help="Results will be stored in this directory. "
        "Defaults to result/${DATE}_${LABEL}")
    out_dir_xor_group.add_argument(
        "--label",
        "--name",
        type=str,
        default=benchmark_cls.NAME,
        help=("Add a name to the default output directory. "
              "Defaults to the benchmark name"))
    return parser

  @classmethod
  def kwargs_from_cli(cls, args: argparse.Namespace) -> Dict[str, Any]:
    if args.out_dir:
      out_dir = args.out_dir
    else:
      label = args.label
      assert label
      cli_dir = pathlib.Path(__file__).parent.parent
      out_dir = cls.get_out_dir(cli_dir, label)
    return {
        "out_dir": out_dir,
        "browsers": args.browser,
        "repetitions": args.repeat,
        "thread_mode": args.parallel,
        "throw": args.throw,
    }

  def __init__(
      self,
      out_dir: pathlib.Path,
      browsers: Sequence[Browser],
      benchmark: Benchmark,
      additional_probes: Iterable[Probe] = (),
      platform: Platform = DEFAULT_PLATFORM,
      env_config: Optional[HostEnvironmentConfig] = None,
      env_validation_mode: ValidationMode = ValidationMode.THROW,  # pytype: disable=annotation-type-mismatch
      repetitions: int = 1,
      timing: Timing = Timing(),
      thread_mode: ThreadMode = ThreadMode.NONE,
      throw: bool = False):
    self.out_dir = out_dir
    assert not self.out_dir.exists(), f"out_dir={self.out_dir} exists already"
    self.out_dir.mkdir(parents=True)
    self._timing = timing
    self.browsers = browsers
    self._validate_browsers()
    self._benchmark = benchmark
    self.stories = benchmark.stories
    self.repetitions = repetitions
    assert self.repetitions > 0, f"Invalid repetitions={self.repetitions}"
    self._probes: List[Probe] = []
    self._runs: List[Run] = []
    self._thread_mode = thread_mode
    self._exceptions = exception.Annotator(throw)
    self._platform = platform
    self._env = HostEnvironment(
        self,  # pytype: disable=wrong-arg-types
        env_config,
        env_validation_mode)
    self._attach_default_probes(additional_probes)
    self._validate_stories()
    self._repetitions_groups: List[RepetitionsRunGroup] = []
    self._story_groups: List[StoriesRunGroup] = []
    self._browser_group: Optional[BrowsersRunGroup] = None

  def _validate_stories(self) -> None:
    for probe_cls in self.stories[0].PROBES:
      assert inspect.isclass(probe_cls), (
          f"Story.PROBES must contain classes only, but got {type(probe_cls)}")
      self.attach_probe(probe_cls())

  def _validate_browsers(self) -> None:
    assert self.browsers, "No browsers provided"
    browser_unique_names = [browser.unique_name for browser in self.browsers]
    assert len(browser_unique_names) == len(set(browser_unique_names)), (
        f"Duplicated browser names in {browser_unique_names}")

  def _attach_default_probes(self, probe_list: Iterable[Probe]) -> None:
    assert len(self._probes) == 0
    self.attach_probe(RunResultsSummaryProbe())
    self.attach_probe(RunDurationsProbe())
    self.attach_probe(RunRunnerLogProbe())
    for probe in probe_list:
      self.attach_probe(probe)

  def attach_probe(self, probe: Probe,
                   matching_browser_only: bool = False) -> Probe:
    assert isinstance(
        probe,
        Probe), (f"Probe must be an instance of Probe, but got {type(probe)}.")
    assert probe not in self._probes, "Cannot add the same probe twice"
    self._probes.append(probe)
    for browser in self.browsers:
      if not probe.is_compatible(browser):
        if matching_browser_only:
          logging.warning("Skipping incompatible probe=%s for browser=%s",
                          probe.name, browser.unique_name)
          continue
        raise Exception(f"Probe '{probe.name}' is not compatible with browser "
                        f"{browser.type}")
      browser.attach_probe(probe)
    return probe

  @property
  def timing(self) -> Timing:
    return self._timing

  @property
  def probes(self) -> List[Probe]:
    return list(self._probes)

  @property
  def exceptions(self) -> exception.Annotator:
    return self._exceptions

  @property
  def is_success(self) -> bool:
    return len(self._runs) > 0 and self._exceptions.is_success

  @property
  def platform(self) -> Platform:
    return self._platform

  @property
  def env(self) -> HostEnvironment:
    return self._env

  @property
  def runs(self) -> List[Run]:
    return self._runs

  @property
  def repetitions_groups(self) -> List[RepetitionsRunGroup]:
    return self._repetitions_groups

  @property
  def story_groups(self) -> List[StoriesRunGroup]:
    return self._story_groups

  @property
  def browser_group(self) -> BrowsersRunGroup:
    assert self._browser_group, f"No BrowsersRunGroup in {self}"
    return self._browser_group

  @property
  def has_browser_group(self) -> bool:
    return self._browser_group is not None

  def sh(self, *args, shell: bool = False, stdout=None):
    return self._platform.sh(*args, shell=shell, stdout=stdout)

  def wait(self, time: Union[float, dt.timedelta],
           absolute_time: bool = False) -> None:
    delta = self.timing.timedelta(time, absolute_time)
    self._platform.sleep(delta)

  def collect_system_details(self) -> None:
    with (self.out_dir / "system_details.json").open(
        "w", encoding="utf-8") as f:
      details = self._platform.system_details()
      json.dump(details, f, indent=2)

  def _setup(self) -> None:
    logging.info("-" * 80)
    logging.info("SETUP")
    logging.info("-" * 80)
    assert self.repetitions > 0, (
        f"Invalid repetitions count: {self.repetitions}")
    assert self.browsers, "No browsers provided: self.browsers is empty"
    assert self.stories, "No stories provided: self.stories is empty"
    logging.info("PREPARING %d BROWSER(S)", len(self.browsers))
    for browser in self.browsers:
      with self._exceptions.capture(f"Preparing browser type={browser.type} "
                                    f"unique_name={browser.unique_name}"):
        browser.setup_binary(self)  # pytype: disable=wrong-arg-types
    self._exceptions.assert_success()
    with self._exceptions.capture("Preparing Runs"):
      self._runs = list(self.get_runs())
      assert self._runs, f"{type(self)}.get_runs() produced no runs"
      logging.info("DISCOVERED %d RUN(S)", len(self._runs))
    self._exceptions.assert_success()
    with self._exceptions.capture("Preparing Environment"):
      self._env.setup()
    with self._exceptions.capture(
        f"Preparing Benchmark: {self._benchmark.NAME}"):
      # TODO: rewrite all imports and hopefully fix this
      self._benchmark.setup(self)  # pytype:  disable=wrong-arg-types
    self.collect_system_details()
    self._exceptions.assert_success()

  def get_runs(self) -> Iterable[Run]:
    index = 0
    for repetition in range(self.repetitions):
      for story in self.stories:
        for browser in self.browsers:
          yield Run(
              self,
              browser,
              story,
              repetition,
              index,
              self.out_dir,
              name=f"{story.name}[{repetition}]",
              throw=self._exceptions.throw)
          index += 1

  def run(self, is_dry_run: bool = False) -> None:
    with helper.SystemSleepPreventer():
      self._setup()
      self._run(is_dry_run)
    if not is_dry_run:
      self._tear_down()
    failed_runs = list(run for run in self.runs if not run.is_success)
    self._exceptions.assert_success(
        f"Runs Failed: {len(failed_runs)}/{len(self.runs)} runs failed.",
        RunnerException)

  def _get_thread_groups(self) -> List[RunThreadGroup]:
    return self._thread_mode.group(self._runs)

  def _run(self, is_dry_run: bool = False) -> None:
    thread_groups: List[RunThreadGroup] = self._get_thread_groups()
    for thread_group in thread_groups:
      thread_group.is_dry_run = is_dry_run
      thread_group.start()
    for thread_group in thread_groups:
      thread_group.join()

  def _tear_down(self) -> None:
    logging.info("=" * 80)
    logging.info("RUNS COMPLETED")
    logging.info("-" * 80)
    logging.info("MERGING PROBE DATA")
    logging.debug("MERGING PROBE DATA: repetitions")
    throw = self._exceptions.throw
    self._repetitions_groups = RepetitionsRunGroup.groups(self._runs, throw)
    with self._exceptions.info("Merging results from multiple repetitions"):
      for repetitions_group in self._repetitions_groups:
        repetitions_group.merge(self)
        self._exceptions.extend(repetitions_group.exceptions, is_nested=True)

    logging.debug("MERGING PROBE DATA: stories")
    self._story_groups = StoriesRunGroup.groups(self._repetitions_groups, throw)
    with self._exceptions.info("Merging results from multiple stories"):
      for story_group in self._story_groups:
        story_group.merge(self)
        self._exceptions.extend(story_group.exceptions, is_nested=True)

    logging.debug("MERGING PROBE DATA: browsers")
    self._browser_group = BrowsersRunGroup(self._story_groups, throw)
    with self._exceptions.info("Merging results from multiple browsers"):
      self._browser_group.merge(self)
      self._exceptions.extend(self._browser_group.exceptions, is_nested=True)

  def cool_down(self) -> None:
    # Cool down between runs
    if not self._platform.is_thermal_throttled():
      return
    logging.info("COOLDOWN")
    for _ in helper.wait_with_backoff(helper.WaitRange(1, 100)):
      if not self._platform.is_thermal_throttled():
        break
      logging.info("COOLDOWN: still hot, waiting some more")


class RunThreadGroup(threading.Thread):

  def __init__(self, runs: List[Run]):
    super().__init__()
    assert len(runs), "Got unexpected empty runs list"
    self._runner: Runner = runs[0].runner
    self._runs = runs
    self.is_dry_run: bool = False

  def start(self) -> None:
    return super().start()

  def run(self) -> None:
    for run in self._runs:
      logging.info("=" * 80)
      logging.info("RUN %s/%s", run.index + 1, len(self._runner.runs))
      logging.info("=" * 80)
      run.run(self.is_dry_run)
      if run.is_success:
        run.log_results()
      else:
        self._runner.exceptions.extend(run.exceptions)


class RunGroup(abc.ABC):

  def __init__(self, throw: bool = False):
    self._exceptions = exception.Annotator(throw)
    self._path: Optional[pathlib.Path] = None
    self._merged_probe_results: Optional[ProbeResultDict] = None

  def _set_path(self, path: pathlib.Path) -> None:
    assert self._path is None
    self._path = path
    self._merged_probe_results = ProbeResultDict(path)

  @property
  def results(self) -> ProbeResultDict:
    assert self._merged_probe_results
    return self._merged_probe_results

  @property
  def path(self) -> pathlib.Path:
    assert self._path
    return self._path

  @property
  def exceptions(self) -> exception.Annotator:
    return self._exceptions

  @property
  @abc.abstractmethod
  def info_stack(self) -> exception.TInfoStack:
    pass

  @property
  @abc.abstractmethod
  def info(self) -> Dict[str, str]:
    pass

  def get_local_probe_result_path(self,
                                  probe: Probe,
                                  exists_ok: bool = False) -> pathlib.Path:
    new_file = self.path / probe.result_path_name
    if not exists_ok:
      assert not new_file.exists(), (
          f"Merged file {new_file} for {self.__class__} exists already.")
    return new_file

  def merge(self, runner: Runner) -> None:
    assert self._merged_probe_results
    with self._exceptions.info(*self.info_stack):
      for probe in reversed(runner.probes):
        with self._exceptions.capture(f"Probe {probe.name} merge results"):
          results = self._merge_probe_results(probe)
          if results is None:
            continue
          self._merged_probe_results[probe] = results

  @abc.abstractmethod
  def _merge_probe_results(self, probe: Probe) -> ProbeResult:
    pass


class RepetitionsRunGroup(RunGroup):
  """
  A group of Run objects that are different repetitions for the same Story with
  and the same browser.
  """

  @classmethod
  def groups(cls, runs: Iterable[Run],
             throw: bool = False) -> List[RepetitionsRunGroup]:
    return list(
        helper.group_by(
            runs,
            key=lambda run: (run.story, run.browser),
            group=lambda _: cls(throw),
            sort_key=None).values())

  def __init__(self, throw: bool = False):
    super().__init__(throw)
    self._runs: List[Run] = []
    self._story: Optional[Story] = None
    self._browser: Optional[Browser] = None

  def append(self, run: Run) -> None:
    if self._path is None:
      self._set_path(run.group_dir)
      self._story = run.story
      self._browser = run.browser
    assert self._story == run.story
    assert self._path == run.group_dir
    assert self._browser == run.browser
    self._runs.append(run)

  @property
  def runs(self) -> Iterable[Run]:
    return self._runs

  @property
  def story(self) -> Story:
    assert self._story
    return self._story

  @property
  def browser(self) -> Browser:
    assert self._browser
    return self._browser

  @property
  def info_stack(self) -> exception.TInfoStack:
    return (f"browser={self.browser.unique_name}", f"story={self.story}")

  @property
  def info(self) -> Dict[str, str]:
    return {"story": str(self.story)}

  def _merge_probe_results(self, probe: Probe) -> ProbeResult:
    # TODO: enable pytype again
    return probe.merge_repetitions(self)  # pytype: disable=wrong-arg-types


class StoriesRunGroup(RunGroup):
  """
  A group of StoryRepetitionsRunGroups for the same browser.
  """

  def __init__(self, throw: bool = False):
    super().__init__(throw)
    self._repetitions_groups: List[RepetitionsRunGroup] = []
    self._browser: Browser = None

  @classmethod
  def groups(cls,
             run_groups: Iterable[RepetitionsRunGroup],
             throw: bool = False) -> List[StoriesRunGroup]:
    return list(
        helper.group_by(
            run_groups,
            key=lambda run_group: run_group.browser,
            group=lambda _: cls(throw),
            sort_key=None).values())

  def append(self, group: RepetitionsRunGroup) -> None:
    if self._path is None:
      self._set_path(group.path.parent)
      self._browser = group.browser
    assert self._path == group.path.parent
    assert self._browser == group.browser
    self._repetitions_groups.append(group)

  @property
  def repetitions_groups(self) -> List[RepetitionsRunGroup]:
    return self._repetitions_groups

  @property
  def runs(self) -> Iterable[Run]:
    for group in self._repetitions_groups:
      yield from group.runs

  @property
  def browser(self) -> Browser:
    return self._browser

  @property
  def info_stack(self) -> exception.TInfoStack:
    return (f"browser={self.browser.unique_name}",)

  @property
  def info(self) -> Dict[str, str]:
    return {
        "label": self.browser.label,
        "browser": self.browser.app_name.title(),
        "version": self.browser.version,
        "os": self.browser.platform.full_version,
        "device": self.browser.platform.device,
        "cpu": self.browser.platform.cpu,
        "binary": str(self.browser.path),
        "flags": str(self.browser.flags)
    }

  @property
  def stories(self) -> Iterable[Story]:
    return (group.story for group in self._repetitions_groups)

  def _merge_probe_results(self, probe: Probe) -> ProbeResult:
    # TODO: enable pytype again
    return probe.merge_stories(self)  # pytype: disable=wrong-arg-types


class BrowsersRunGroup(RunGroup):
  _story_groups: Iterable[StoriesRunGroup]

  def __init__(self, story_groups, throw: bool) -> None:
    super().__init__(throw)
    self._story_groups = story_groups
    self._set_path(story_groups[0].path.parent)

  @property
  def story_groups(self) -> Iterable[StoriesRunGroup]:
    return self._story_groups

  @property
  def browsers(self) -> Iterable[Browser]:
    for story_group in self._story_groups:
      yield story_group.browser

  @property
  def repetitions_groups(self) -> Iterable[RepetitionsRunGroup]:
    for story_group in self._story_groups:
      yield from story_group.repetitions_groups

  @property
  def runs(self) -> Iterable[Run]:
    for group in self._story_groups:
      yield from group.runs

  @property
  def info_stack(self) -> exception.TInfoStack:
    return ()

  @property
  def info(self) -> Dict[str, str]:
    return {}

  def _merge_probe_results(self, probe: Probe) -> ProbeResult:
    # TODO: enable pytype again
    return probe.merge_browsers(self)  # pytype: disable=wrong-arg-types


class Run:
  # TODO: use enum class
  STATE_INITIAL = 0
  STATE_PREPARE = 1
  STATE_RUN = 2
  STATE_DONE = 3

  def __init__(self,
               runner: Runner,
               browser: Browser,
               story: Story,
               repetition: int,
               index: int,
               root_dir: pathlib.Path,
               name: Optional[str] = None,
               temperature: Optional[int] = None,
               throw: bool = False):
    self._state = self.STATE_INITIAL
    self._run_success: Optional[bool] = None
    self._runner = runner
    self._browser = browser
    self._story = story
    assert repetition >= 0
    self._repetition = repetition
    assert index >= 0
    self._index = index
    self._name = name
    self._out_dir = self.get_out_dir(root_dir).absolute()
    self._probe_results = ProbeResultDict(self._out_dir)
    self._extra_js_flags = JSFlags()
    self._extra_flags = Flags()
    self._durations = helper.Durations()
    self._temperature = temperature
    self._exceptions = exception.Annotator(throw)
    self._browser_tmp_dir: Optional[pathlib.Path] = None

  def __str__(self) -> str:
    return f"Run({self.name}, {self._state}, {self.browser})"

  def get_out_dir(self, root_dir: pathlib.Path) -> pathlib.Path:
    return root_dir / self.browser.unique_name / self.story.name / str(
        self._repetition)

  @property
  def group_dir(self) -> pathlib.Path:
    return self.out_dir.parent

  def actions(self, name: str, verbose: bool = False) -> Actions:
    return Actions(name, self, verbose=verbose)

  @property
  def info_stack(self) -> exception.TInfoStack:
    return (
        f"Run({self.name})",
        (f"browser={self.browser.type} label={self.browser.label} "
         "binary={self.browser.path}"),
        f"story={self.story}",
        f"repetition={self.repetition}",
    )

  def details_json(self) -> Dict[str, Any]:
    details = {
        "name": self.name,
        "repetition": self.repetition,
        "temperature": self.temperature,
        "story": str(self.story),
        "duration": dt.timedelta(seconds=self.story.duration),
        "probes": [probe.name for probe in self.probes]
    }
    return details

  @property
  def temperature(self) -> Optional[int]:
    return self._temperature

  @property
  def durations(self) -> helper.Durations:
    return self._durations

  @property
  def repetition(self) -> int:
    return self._repetition

  @property
  def index(self) -> int:
    return self._index

  @property
  def runner(self) -> Runner:
    return self._runner

  @property
  def timing(self) -> Timing:
    return self.runner.timing

  @property
  def browser(self) -> Browser:
    return self._browser

  @property
  def platform(self) -> Platform:
    return self.browser_platform

  @property
  def browser_platform(self) -> Platform:
    return self._browser.platform

  @property
  def runner_platform(self) -> Platform:
    return self.runner.platform

  @property
  def is_remote(self) -> bool:
    return self.browser_platform.is_remote

  @property
  def out_dir(self) -> pathlib.Path:
    """A local directory where all result files are gathered.
    Results from browsers on remote platforms are transferred to this dir
    as well."""
    return self._out_dir

  @property
  def browser_tmp_dir(self) -> pathlib.Path:
    """Returns a path to a tmp dir on the browser platform."""
    if not self._browser_tmp_dir:
      prefix = "cb_run_results"
      self._browser_tmp_dir = self.browser_platform.mkdtemp(prefix)
    return self._browser_tmp_dir

  @property
  def results(self) -> ProbeResultDict:
    return self._probe_results

  @property
  def story(self) -> Story:
    return self._story

  @property
  def name(self) -> Optional[str]:
    return self._name

  @property
  def extra_js_flags(self) -> JSFlags:
    return self._extra_js_flags

  @property
  def extra_flags(self) -> Flags:
    return self._extra_flags

  @property
  def probes(self) -> Iterable[Probe]:
    return self._runner.probes

  @property
  def exceptions(self) -> exception.Annotator:
    return self._exceptions

  @property
  def is_success(self) -> bool:
    return self._exceptions.is_success

  @contextlib.contextmanager
  def measure(
      self, label: str
  ) -> Iterator[Tuple[exception.ExceptionAnnotationScope,
                      helper.DurationMeasureContext]]:
    # Return a combined context manager that adds an named exception info
    # and measures the time during the with-scope.
    with self._exceptions.info(label) as stack, self._durations.measure(
        label) as timer:
      yield (stack, timer)

  def exception_info(self,
                     *stack_entries: str) -> exception.ExceptionAnnotationScope:
    return self._exceptions.info(*stack_entries)

  def exception_handler(self,
                        *stack_entries: str,
                        exceptions: exception.TExceptionTypes = (Exception,)
                       ) -> exception.ExceptionAnnotationScope:
    return self._exceptions.capture(*stack_entries, exceptions=exceptions)

  def get_browser_details_json(self) -> Dict[str, Any]:
    details_json = self.browser.details_json()
    details_json["js_flags"] += tuple(self.extra_js_flags.get_list())
    details_json["flags"] += tuple(self.extra_flags.get_list())
    return details_json

  def get_default_probe_result_path(self, probe: Probe) -> pathlib.Path:
    """Return a local or remote/browser-based result path depending on the
    Probe default RESULT_LOCATION."""
    if probe.RESULT_LOCATION == ResultLocation.BROWSER:
      return self.get_browser_probe_result_path(probe)
    if probe.RESULT_LOCATION == ResultLocation.LOCAL:
      return self.get_local_probe_result_path(probe)
    raise ValueError(f"Invalid probe.RESULT_LOCATION {probe.RESULT_LOCATION} "
                     f"for probe {probe}")

  def get_local_probe_result_path(self, probe: Probe) -> pathlib.Path:
    file = self._out_dir / probe.result_path_name
    assert not file.exists(), f"Probe results file exists already. file={file}"
    return file

  def get_browser_probe_result_path(self, probe: Probe) -> pathlib.Path:
    """Returns a temporary path on the remote browser or the same as 
    get_local_probe_result_path() on a local browser."""
    if not self.is_remote:
      return self.get_local_probe_result_path(probe)
    path = self.browser_tmp_dir / probe.result_path_name
    self.browser_platform.mkdir(path.parent)
    logging.debug("Creating remote result dir=%s on platform=%s", path.parent,
                  self.browser_platform)
    return path

  def setup(self, is_dry_run: bool) -> List[ProbeScope[Any]]:
    self._advance_state(self.STATE_INITIAL, self.STATE_PREPARE)
    logging.debug("PREPARE")
    logging.info("STORY: %s", self.story)
    logging.info("STORY DURATION: %ss",
                 self.timing.timedelta(self.story.duration))
    logging.info("RUN DIR: %s", self._out_dir)

    if is_dry_run:
      logging.info("BROWSER: %s", self.browser.path)
      return []

    self._run_success = None
    browser_log_file = self._out_dir / "browser.log"
    assert not browser_log_file.exists(), (
        f"Default browser log file {browser_log_file} already exists.")
    self._browser.set_log_file(browser_log_file)

    with self.measure("runner-cooldown"):
      self._runner.wait(self._runner.timing.cool_down_time, absolute_time=True)
      self._runner.cool_down()

    probe_run_scopes: List[ProbeScope] = []
    with self.measure("probes-creation"):
      probe_set = set()
      for probe in self.probes:
        assert probe not in probe_set, (
            f"Got duplicate probe name={probe.name}")
        probe_set.add(probe)
        if probe.PRODUCES_DATA:
          self._probe_results[probe] = EmptyProbeResult()
        assert probe.is_attached, (
            f"Probe {probe.name} is not properly attached to a browser")
        probe_run_scopes.append(probe.get_scope(self))

    with self.measure("probes-setup"):
      for probe_scope in probe_run_scopes:
        with self.exception_info(f"Probe {probe_scope.name} setup"):
          probe_scope.setup(self)  # pytype: disable=wrong-arg-types

    with self.measure("browser-setup"):
      try:
        # pytype somehow gets the package path wrong here, disabling for now.
        self._browser.setup(self)  # pytype: disable=wrong-arg-types
      except:
        # Clean up half-setup browser instances
        self._browser.force_quit()
        raise
    return probe_run_scopes

  def run(self, is_dry_run: bool = False) -> None:
    self._out_dir.mkdir(parents=True, exist_ok=True)
    with helper.ChangeCWD(self._out_dir), self.exception_info(*self.info_stack):
      probe_scopes = self.setup(is_dry_run)
      self._advance_state(self.STATE_PREPARE, self.STATE_RUN)
      self._run_success = False
      logging.debug("CWD %s", self._out_dir)
      try:
        self._run(probe_scopes, is_dry_run)
      except Exception as e:  # pylint: disable=broad-except
        self._exceptions.append(e)
      finally:
        if not is_dry_run:
          self.tear_down(probe_scopes)

  def _run(self, probe_scopes: Sequence[ProbeScope], is_dry_run: bool) -> None:
    probe_start_time = dt.datetime.now()
    probe_scope_manager = contextlib.ExitStack()

    for probe_scope in probe_scopes:
      probe_scope.set_start_time(probe_start_time)
      probe_scope_manager.enter_context(probe_scope)

    with probe_scope_manager:
      self._durations["probes-start"] = (dt.datetime.now() - probe_start_time)
      logging.info("RUNNING STORY")
      assert self._state == self.STATE_RUN, "Invalid state"
      try:
        with self.measure("run"), helper.Spinner():
          if not is_dry_run:
            self._story.run(self)
        self._run_success = True
      except TimeoutError as e:
        # Handle TimeoutError earlier since they might be caused by
        # throttled down non-foreground browser.
        self._exceptions.append(e)
      self._check_browser_foreground()

  def _check_browser_foreground(self) -> None:
    if not self.browser.pid or self.browser.viewport.is_headless:
      return
    info = self.platform.foreground_process()
    if not info:
      return
    assert info["pid"] == self.browser.pid, (
        f"Browser(name={self.browser.unique_name} pid={self.browser.pid})) "
        "was not in the foreground at the end of the benchmark. "
        "Background apps and tabs can be heavily throttled.")

  def _advance_state(self, expected: int, next_state: int) -> None:
    assert self._state == expected, (
        f"Invalid state got={self._state} expected={expected}")
    self._state = next_state

  def tear_down(self,
                probe_scopes: List[ProbeScope],
                is_shutdown: bool = False) -> None:
    self._advance_state(self.STATE_RUN, self.STATE_DONE)
    with self.measure("browser-tear_down"):
      if self._browser.is_running is False:
        logging.warning("Browser is no longer running (crashed or closed).")
      else:
        if is_shutdown:
          try:
            self._browser.quit(self._runner)  # pytype: disable=wrong-arg-types
          except Exception as e:  # pylint: disable=broad-except
            logging.warning("Error quitting browser: %s", e)
            return
        with self._exceptions.capture("Quit browser"):
          self._browser.quit(self._runner)  # pytype: disable=wrong-arg-types
    with self.measure("probes-tear_down"):
      logging.debug("TEARDOWN")
      self._tear_down_probe_scopes(probe_scopes)
    self._rm_browser_tmp_dir()

  def _tear_down_probe_scopes(self, probe_scopes: List[ProbeScope]) -> None:
    for probe_scope in reversed(probe_scopes):
      with self.exceptions.capture(f"Probe {probe_scope.name} teardown"):
        assert probe_scope.run == self
        probe_results: ProbeResult = probe_scope.tear_down(self)  # pytype: disable=wrong-arg-types
        probe = probe_scope.probe
        if probe_results.is_empty:
          logging.warning("Probe did not extract any data. probe=%s run=%s",
                          probe, self)
        self._probe_results[probe] = probe_results

  def _rm_browser_tmp_dir(self) -> None:
    if not self._browser_tmp_dir:
      return
    self.browser_platform.rm(self._browser_tmp_dir, dir=True)

  def log_results(self) -> None:
    for probe in self.probes:
      probe.log_run_result(self)


class Actions(helper.TimeScope):

  def __init__(self,
               message: str,
               run: Run,
               runner: Optional[Runner] = None,
               browser: Optional[Browser] = None,
               verbose: bool = False):
    assert message, "Actions need a name"
    super().__init__(message)
    self._exception_annotation = run.exceptions.info(f"Action: {message}")
    self._run: Run = run
    self._browser: Browser = browser or run.browser
    self._runner: Runner = runner or run.runner
    self._is_active: bool = False
    self._verbose: bool = verbose

  @property
  def timing(self) -> Timing:
    return self._runner.timing

  @property
  def run(self) -> Run:
    return self._run

  @property
  def platform(self) -> Platform:
    return self._run.platform

  def __enter__(self) -> Actions:
    self._exception_annotation.__enter__()
    super().__enter__()
    self._is_active = True
    logging.debug("Action begin: %s", self._message)
    if self._verbose:
      logging.info(self._message)
    else:
      # Print message that doesn't overlap with helper.Spinner
      sys.stdout.write(f"   {self._message}\r")
    return self

  def __exit__(self, exc_type, exc_value, exc_traceback) -> None:
    self._is_active = False
    self._exception_annotation.__exit__(exc_type, exc_value, exc_traceback)
    logging.debug("Action end: %s", self._message)
    super().__exit__(exc_type, exc_value, exc_traceback)

  def _assert_is_active(self) -> None:
    assert self._is_active, "Actions have to be used in a with scope"

  def js(self,
         js_code: str,
         timeout: Union[float, int] = 10,
         arguments: Sequence[object] = (),
         **kwargs) -> Any:
    self._assert_is_active()
    assert js_code, "js_code must be a valid JS script"
    if kwargs:
      js_code = js_code.format(**kwargs)
    delta = self.timing.timedelta(timeout)
    return self._browser.js(
        self._runner,  # pytype: disable=wrong-arg-types
        js_code,
        delta,
        arguments=arguments)

  def wait_js_condition(self, js_code: str, min_wait: float,
                        timeout: float) -> None:
    wait_range = helper.WaitRange(
        self.timing.timedelta(min_wait), self.timing.timedelta(timeout))
    assert "return" in js_code, (
        f"Missing return statement in js-wait code: {js_code}")
    for _, time_left in helper.wait_with_backoff(wait_range):
      time_units = self.timing.units(time_left)
      result = self.js(js_code, timeout=time_units, absolute_time=True)
      if result:
        return
      assert result is False, (
          f"js_code did not return a bool, but got: {result}\n"
          f"js-code: {js_code}")

  def show_url(self, url: str) -> None:
    self._assert_is_active()
    self._browser.show_url(
        self._runner,  # pytype: disable=wrong-arg-types
        url)

  def wait(self, seconds: float = 1) -> None:
    self._assert_is_active()
    self.platform.sleep(seconds)
