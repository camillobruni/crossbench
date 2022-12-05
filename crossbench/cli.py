# Copyright 2022 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from __future__ import annotations

import argparse
import io
import itertools
import json
import logging
import pathlib
import plistlib
import re
import shutil
import sys
import tempfile
import textwrap
from typing import (Any, Dict, Iterable, List, Optional, Sequence, Tuple, Type,
                    Union)

import hjson
from tabulate import tabulate

import crossbench
import crossbench.benchmarks
import crossbench.benchmarks.all
import crossbench.browsers
from crossbench.browsers.chrome import ChromeDownloader
import crossbench.env
import crossbench.exception
import crossbench.flags
import crossbench.probes
import crossbench.probes.all
import crossbench.runner

#TODO: fix imports
cb = crossbench


def _map_flag_group_item(flag_name: str, flag_value: Optional[str]):
  if flag_value is None:
    return None
  if flag_value == "":
    return (flag_name, None)
  return (flag_name, flag_value)


class ConfigFileError(ValueError):
  pass


class FlagGroupConfig:
  """This object corresponds to a flag-group in a configuration file.
  It contains mappings from flags to multiple values.
  """

  _variants: Dict[str, Iterable[Optional[str]]]
  name: str

  def __init__(self, name: str,
               variants: Dict[str, Union[Iterable[Optional[str]], str]]):
    self.name = name
    self._variants = {}
    for flag_name, flag_variants_or_value in variants.items():
      assert flag_name not in self._variants
      assert flag_name
      if isinstance(flag_variants_or_value, str):
        self._variants[flag_name] = (str(flag_variants_or_value),)
      else:
        assert isinstance(flag_variants_or_value, Iterable)
        flag_variants = tuple(flag_variants_or_value)
        assert len(flag_variants) == len(set(flag_variants)), (
            "Flag variant contains duplicate entries: {flag_variants}")
        self._variants[flag_name] = tuple(flag_variants_or_value)

  def get_variant_items(self) -> Iterable[Optional[Tuple[str, Optional[str]]]]:
    for flag_name, flag_values in self._variants.items():
      yield tuple(
          _map_flag_group_item(flag_name, flag_value)
          for flag_value in flag_values)


BrowserLookupTable = Dict[str, Tuple[Type[cb.browsers.Browser], pathlib.Path]]


class BrowserConfig:

  @classmethod
  def from_cli_args(cls, args) -> BrowserConfig:
    browser_config = BrowserConfig()
    if args.browser_config:
      path = args.browser_config.expanduser()
      with path.open(encoding="utf-8") as f:
        browser_config.load(f)
    else:
      browser_config.load_from_args(args)
    return browser_config

  def __init__(self,
               raw_config_data: Optional[Dict] = None,
               browser_lookup_override: Optional[BrowserLookupTable] = None):
    self.flag_groups: Dict[str, FlagGroupConfig] = {}
    self._variants: List[cb.browsers.Browser] = []
    self._browser_lookup_override = browser_lookup_override or {}
    self._exceptions = crossbench.exception.Annotator()
    if raw_config_data:
      self.load_dict(raw_config_data)

  @property
  def variants(self) -> List[cb.browsers.Browser]:
    self._exceptions.assert_success(
        "Could not create variants from config files: {}", ConfigFileError)
    return self._variants

  def load(self, f):
    with self._exceptions.capture(f"Loading browser config file: {f.name}"):
      with self._exceptions.info(f"Parsing {hjson.__name__}"):
        config = hjson.load(f)
      with self._exceptions.info(f"Parsing config file: {f.name}"):
        self.load_dict(config)

  def load_dict(self, raw_config_data: Dict):
    try:
      if "flags" in raw_config_data:
        with self._exceptions.info("Parsing config['flags']"):
          self._parse_flag_groups(raw_config_data["flags"])
      if "browsers" not in raw_config_data:
        raise ConfigFileError("Config does not provide a 'browsers' dict.")
      if not raw_config_data["browsers"]:
        raise ConfigFileError("Config contains empty 'browsers' dict.")
      with self._exceptions.info("Parsing config['browsers']"):
        self._parse_browsers(raw_config_data["browsers"])
    except Exception as e:  # pylint: disable=broad-except
      self._exceptions.append(e)

  def _parse_flag_groups(self, data: Dict[str, Any]):
    for flag_name, group_config in data.items():
      with self._exceptions.capture(
          f"Parsing flag-group: flags['{flag_name}']"):
        self._parse_flag_group(flag_name, group_config)

  def _parse_flag_group(self, name, raw_flag_group_data):
    if name in self.flag_groups:
      raise ConfigFileError(f"flag-group flags['{name}'] exists already")
    variants: Dict[str, List[str]] = {}
    for flag_name, values in raw_flag_group_data.items():
      if not flag_name.startswith("-"):
        raise ConfigFileError(f"Invalid flag name: '{flag_name}'")
      if flag_name not in variants:
        flag_values = variants[flag_name] = []
      else:
        flag_values = variants[flag_name]
      if isinstance(values, str):
        values = [values]
      for value in values:
        if value == "None,":
          raise ConfigFileError(
              f"Please use null instead of None for flag '{flag_name}' ")
        # O(n^2) check, assuming very few values per flag.
        if value in flag_values:
          raise ConfigFileError(
              "Same flag variant was specified more than once: "
              f"'{value}' for entry '{flag_name}'")
        flag_values.append(value)
    self.flag_groups[name] = FlagGroupConfig(name, variants)

  def _parse_browsers(self, data: Dict[str, Any]):
    for name, browser_config in data.items():
      with self._exceptions.info(f"Parsing browsers['{name}']"):
        self._parse_browser(name, browser_config)
    self._ensure_unique_browser_names()

  def _parse_browser(self, name, raw_browser_data):
    path_or_identifier = raw_browser_data["path"]
    if path_or_identifier in self._browser_lookup_override:
      browser_cls, path = self._browser_lookup_override[path_or_identifier]
    else:
      path = self._get_browser_path(path_or_identifier)
      browser_cls = self._get_browser_cls_from_path(path)
    if not path.exists():
      raise ConfigFileError(f"browsers['{name}'].path='{path}' does not exist.")
    with self._exceptions.info(f"Parsing browsers['{name}'].flags"):
      raw_flags = self._parse_flags(name, raw_browser_data)
    with self._exceptions.info(
        f"Expand browsers['{name}'].flags into full variants"):
      variants_flags = tuple(
          browser_cls.default_flags(flags) for flags in raw_flags)
    logging.info("SELECTED BROWSER: '%s' with %s flag variants:", name,
                 len(variants_flags))
    for i in range(len(variants_flags)):
      logging.info("   %s: %s", i, variants_flags[i])
    # pytype: disable=not-instantiable
    self._variants += [
        browser_cls(
            label=self._flags_to_label(name, flags), path=path, flags=flags)
        for flags in variants_flags
    ]
    # pytype: enable=not-instantiable

  def _flags_to_label(self, name: str, flags: cb.flags.Flags) -> str:
    return f"{name}_{cb.browsers.convert_flags_to_label(*flags.get_list())}"

  def _parse_flags(self, name, data):
    flags_product = []
    flag_group_names = data.get("flags", [])
    if isinstance(flag_group_names, str):
      flag_group_names = [flag_group_names]
    if not isinstance(flag_group_names, list):
      raise ConfigFileError(f"'flags' is not a list for browser='{name}'")
    seen_flag_group_names = set()
    for flag_group_name in flag_group_names:
      if flag_group_name in seen_flag_group_names:
        raise ConfigFileError(
            f"Duplicate group name '{flag_group_name}' for browser='{name}'")
      seen_flag_group_names.add(flag_group_name)
      # Use temporary FlagGroupConfig for inline fixed flag definition
      if flag_group_name.startswith("--"):
        flag_name, flag_value = cb.flags.Flags.split(flag_group_name)
        # No-value-flags produce flag_value == None, convert this to the "" for
        # compatibility with the flag variants, where None would mean removing
        # the flag.
        if flag_value is None:
          flag_value = ""
        flag_group = FlagGroupConfig("temporary", {flag_name: flag_value})
        assert flag_group_name not in self.flag_groups
      else:
        flag_group = self.flag_groups.get(flag_group_name, None)
        if flag_group is None:
          raise ConfigFileError(f"group='{flag_group_name}' "
                                f"for browser='{name}' does not exist.")
      flags_product += flag_group.get_variant_items()
    if len(flags_product) == 0:
      # use empty default
      return ({},)
    flags_product = itertools.product(*flags_product)
    # Filter out (.., None) value
    flags_product = list(
        list(flag_item
             for flag_item in flags_items
             if flag_item is not None)
        for flags_items in flags_product)
    assert flags_product
    return flags_product

  def _get_browser_cls_from_path(self, path: pathlib.Path
                                ) -> Type[cb.browsers.Browser]:
    path_str = str(path).lower()
    if "safari" in path_str:
      return cb.browsers.SafariWebDriver
    if "chrome" in path_str:
      return cb.browsers.ChromeWebDriver
    if "firefox" in path_str:
      return cb.browsers.FirefoxWebDriver
    if "edge" in path_str:
      return cb.browsers.EdgeWebDriver
    raise ValueError(f"Unsupported browser='{path}'")

  def _ensure_unique_browser_names(self):
    if self._has_unique_variant_names():
      return
    # Expand to full version names
    for browser in self._variants:
      browser.unique_name = f"{browser.type}_{browser.version}_{browser.label}"
    if self._has_unique_variant_names():
      return
    logging.info("Got unique browser names and versions, "
                 "please use --browser-config for more meaningful names")
    # Last resort, add index
    for index, browser in enumerate(self._variants):
      browser.unique_name += f"_{index}"
    assert self._has_unique_variant_names()

  def _has_unique_variant_names(self):
    names = [browser.unique_name for browser in self._variants]
    unique_names = set(names)
    return len(unique_names) == len(names)

  def load_from_args(self, args):
    browsers = args.browser or ["chrome-stable"]
    assert isinstance(browsers, list)
    if len(browsers) != len(set(browsers)):
      raise ValueError(f"Got duplicate --browser arguments: {browsers}")
    for browser in browsers:
      self._append_browser(args, browser)
    self._verify_browser_flags(args)
    self._ensure_unique_browser_names()

  def _verify_browser_flags(self, args):
    if len(self._variants) == 1:
      return
    chrome_args = {
        "--enable-features": args.enable_features,
        "--disable-features": args.disable_features,
        "--js-flags": args.js_flags
    }
    for flag_name, value in chrome_args.items():
      if not value:
        continue
      for browser in self._variants:
        if not isinstance(browser, cb.browsers.Chromium):
          raise ValueError(f"Used chrome/chromium-specific flags {flag_name} "
                           f"for non-chrome {browser.unique_name}.\n"
                           "Use --browser-config for complex variants.")
    if not args.other_browser_args:
      return
    browser_types = set(browser.type for browser in self._variants)
    if len(browser_types) > 1:
      raise ValueError(f"Multiple browser types {browser_types} "
                       "cannot be used with common extra browser flags: "
                       f"{args.other_browser_args}.\n"
                       "Use --browser-config for complex variants.")

  def _append_browser(self, args, browser: str):
    assert browser, "Expected non-empty browser name"
    path = self._get_browser_path(browser)
    browser_cls = self._get_browser_cls_from_path(path)
    flags = browser_cls.default_flags()

    if issubclass(browser_cls, cb.browsers.Chromium):
      assert isinstance(flags, cb.flags.ChromeFlags)
      self._init_chrome_flags(args, flags)

    for flag_str in args.other_browser_args:
      flags.set(*cb.flags.Flags.split(flag_str))

    label = cb.browsers.convert_flags_to_label(*flags.get_list())
    browser_instance = browser_cls(  # pytype: disable=not-instantiable
        label=label,
        path=path,
        flags=flags)
    logging.info("SELECTED BROWSER: name=%s path='%s' ",
                 browser_instance.unique_name, path)
    self._variants.append(browser_instance)

  def _init_chrome_flags(self, args, flags: cb.flags.ChromeFlags):
    if args.enable_features:
      for feature in args.enable_features.split(","):
        flags.features.enable(feature)
    if args.disable_features:
      for feature in args.disable_features.split(","):
        flags.features.disable(feature)
    if args.js_flags:
      flags.js_flags.update(args.js_flags.split(","))

  def _get_browser_path(self, path_or_identifier: str) -> pathlib.Path:
    identifier = path_or_identifier.lower()
    # We're not using a dict-based lookup here, since not all browsers are
    # available on all platforms
    if identifier in ("chrome", "chrome-stable", "stable"):
      return cb.browsers.Chrome.stable_path()
    if identifier in ("chrome-beta", "beta"):
      return cb.browsers.Chrome.beta_path()
    if identifier in ("chrome-dev", "dev"):
      return cb.browsers.Chrome.dev_path()
    if identifier in ("chrome-canary", "canary"):
      return cb.browsers.Chrome.canary_path()
    if identifier in ("edge", "edge-stable"):
      return cb.browsers.Edge.stable_path()
    if identifier == "edge-beta":
      return cb.browsers.Edge.beta_path()
    if identifier == "edge-dev":
      return cb.browsers.Edge.dev_path()
    if identifier == "edge-canary":
      return cb.browsers.Edge.canary_path()
    if identifier in ("safari", "sf"):
      return cb.browsers.Safari.default_path()
    if identifier in ("safari-technology-preview", "sf-tp", "tp"):
      return cb.browsers.Safari.technology_preview_path()
    if identifier in ("firefox", "ff"):
      return cb.browsers.Firefox.default_path()
    if identifier in ("firefox-dev", "firefox-developer-edition", "ff-dev"):
      return cb.browsers.Firefox.developer_edition_path()
    if identifier in ("firefox-nightly", "ff-nightly", "ff-trunk"):
      return cb.browsers.Firefox.nightly_path()
    if ChromeDownloader.VERSION_RE.match(identifier):
      return ChromeDownloader.load(identifier)
    path = pathlib.Path(path_or_identifier)
    if path.exists():
      return path
    path = path.expanduser()
    if path.exists():
      return path
    if len(path.parts) > 1:
      raise ValueError(f"Browser at '{path}' does not exist.")
    raise ValueError(
        f"Unknown browser path or short name: '{path_or_identifier}'")


class ProbeConfig:

  LOOKUP: Dict[str, Type[cb.probes.Probe]] = {
      cls.NAME: cls for cls in cb.probes.all.GENERAL_PURPOSE_PROBES
  }

  @classmethod
  def from_cli_args(cls, args) -> ProbeConfig:
    if args.probe_config:
      with args.probe_config.open(encoding="utf-8") as f:
        return cls.load(f)
    return cls(args.probe)

  @classmethod
  def load(cls, file) -> ProbeConfig:
    probe_config = cls()
    probe_config.load_config_file(file)
    return probe_config

  def __init__(self, probe_names_with_args: Optional[Iterable[str]] = None):
    self._exceptions = cb.exception.Annotator(throw=True)
    self._probes: List[cb.probes.Probe] = []
    if not probe_names_with_args:
      return
    for probe_name_with_args in probe_names_with_args:
      with self._exceptions.capture(f"Parsing --probe={probe_name_with_args}"):
        self.add_probe(probe_name_with_args)

  @property
  def probes(self) -> List[cb.probes.Probe]:
    self._exceptions.assert_success(
        "Could not load probe config from files: {}", ConfigFileError)
    return self._probes

  def add_probe(self, probe_name_with_args: str):
    # look for "ProbeName{json_key:json_value, ...}"
    inline_config = {}
    if probe_name_with_args[-1] == "}":
      probe_name, json_args = probe_name_with_args.split("{", maxsplit=1)
      assert json_args[-1] == "}"
      inline_config = hjson.loads("{" + json_args)
    else:
      # Default case without the additional hjson payload
      probe_name = probe_name_with_args
    if probe_name not in self.LOOKUP:
      self.raise_unknown_probe(probe_name)
    probe_cls: Type[cb.probes.Probe] = self.LOOKUP[probe_name]
    self._probes.append(probe_cls.from_config(inline_config))

  def load_config_file(self, file):
    with self._exceptions.capture(f"Loading probe config file: {file.name}"):
      with self._exceptions.info(f"Parsing {hjson.__name__}"):
        data = hjson.load(file)
      if "probes" not in data:
        raise ValueError(
            "Probe config file does not contain a 'probes' dict value.")
      self.load_dict(data["probes"])

  def load_dict(self, data: Dict[str, Any]):
    for probe_name, config_data in data.items():
      with self._exceptions.info(
          f"Parsing probe config probes['{probe_name}']"):
        if probe_name not in self.LOOKUP:
          self.raise_unknown_probe(probe_name)
        probe_cls = self.LOOKUP[probe_name]
        self._probes.append(probe_cls.from_config(config_data))

  def raise_unknown_probe(self, probe_name: str):
    raise ValueError(f"Unknown probe name: '{probe_name}'\n"
                     f"Options are: {list(self.LOOKUP.keys())}")


def existing_file_type(str_value):
  try:
    path = pathlib.Path(str_value).expanduser()
  except RuntimeError as e:
    raise argparse.ArgumentTypeError(f"Invalid Path '{str_value}': {e}") from e
  if not path.exists():
    raise argparse.ArgumentTypeError(f"Path '{path}', does not exist.")
  if not path.is_file():
    raise argparse.ArgumentTypeError(f"Path '{path}', is not a file.")
  return path


def inline_env_config(value: str) -> cb.env.HostEnvironmentConfig:
  if value in cb.env.HostEnvironment.CONFIGS:
    return cb.env.HostEnvironment.CONFIGS[value]
  if value[0] != "{":
    raise argparse.ArgumentTypeError(
        f"Invalid env config name: '{value}'. "
        f"choices = {list(cb.env.HostEnvironment.CONFIGS.keys())}")
  # Assume hjson data
  kwargs = None
  msg = ""
  try:
    kwargs = hjson.loads(value)
    return cb.env.HostEnvironmentConfig(**kwargs)
  except Exception as e:
    msg = f"\n{e}"
    raise argparse.ArgumentTypeError(
        f"Invalid inline config string: {value}{msg}") from e


def env_config_file(value: str) -> cb.env.HostEnvironmentConfig:
  config_path = existing_file_type(value)
  try:
    with config_path.open(encoding="utf-8") as f:
      data = hjson.load(f)
    if "env" not in data:
      raise argparse.ArgumentTypeError("No 'env' property found")
    kwargs = data["env"]
    return cb.env.HostEnvironmentConfig(**kwargs)
  except Exception as e:
    msg = f"\n{e}"
    raise argparse.ArgumentTypeError(
        f"Invalid env config file: {value}{msg}") from e


BenchmarkClsT = Type[cb.benchmarks.Benchmark]


class CrossBenchCLI:

  BENCHMARKS: Tuple[Tuple[BenchmarkClsT, Tuple[str, ...]], ...] = (
      (cb.benchmarks.all.Speedometer20Benchmark, ()),
      (cb.benchmarks.all.Speedometer21Benchmark, ("speedometer",)),
      (cb.benchmarks.all.JetStream2Benchmark, ("jetstream",)),
      (cb.benchmarks.all.MotionMark12Benchmark, ("motionmark",)),
      (cb.benchmarks.all.PageLoadBenchmark, ()),
  )

  RUNNER_CLS: Type[cb.runner.Runner] = cb.runner.Runner

  def __init__(self):
    self._subparsers: Dict[BenchmarkClsT, argparse.ArgumentParser] = {}
    self.parser = argparse.ArgumentParser()
    self._setup_parser()
    self._setup_subparser()

  def _setup_parser(self):
    self._add_verbosity_argument(self.parser)
    # Disable colors by default when piped to a file.
    has_color = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    self.parser.add_argument(
        "--no-color",
        dest="color",
        action="store_false",
        default=has_color,
        help="Disable colored output")

  def _add_verbosity_argument(self, parser):
    parser.add_argument(
        "-v",
        "--verbose",
        dest="verbosity",
        action="count",
        default=0,
        help="Increase output verbosity (0..2)")

  def _setup_subparser(self):
    self.subparsers = self.parser.add_subparsers(
        title="Subcommands", dest="subcommand", required=True)
    for benchmark_cls, alias in self.BENCHMARKS:
      self._setup_benchmark_subparser(benchmark_cls, alias)
    self._setup_describe_subparser()

  def _setup_describe_subparser(self):
    describe_parser = self.subparsers.add_parser(
        "describe", aliases=["desc"], help="Print all benchmarks and stories")
    describe_parser.add_argument(
        "category",
        nargs="?",
        choices=["all", "benchmark", "benchmarks", "probe", "probes"],
        default="all",
        help="Limit output to the given category, defaults to 'all'")
    describe_parser.add_argument(
        "filter",
        nargs="?",
        help=("Only display the given item from the provided category. "
              "By default all items are displayed. "
              "Example: describe probes v8.log"))
    describe_parser.add_argument("--json",
                                 default=False,
                                 action="store_true",
                                 help="Print the data as json data")
    describe_parser.set_defaults(subcommand=self.describe_subcommand)
    self._add_verbosity_argument(describe_parser)

  def describe_subcommand(self, args: argparse.Namespace):
    benchmarks_data: Dict[str, Any] = {}
    for benchmark_cls, aliases in self.BENCHMARKS:
      if args.filter and benchmark_cls.NAME != args.filter:
        continue
      benchmark_info = benchmark_cls.describe()
      benchmark_info["aliases"] = aliases or "None"
      benchmark_info["help"] = f"See `{benchmark_cls.NAME} --help`"
      benchmarks_data[benchmark_cls.NAME] = benchmark_info
    data = {
        "benchmarks": benchmarks_data,
        "probes": {
            probe_cls.NAME: probe_cls.help_text()
            for probe_cls in cb.probes.all.GENERAL_PURPOSE_PROBES
            if not args.filter or probe_cls.NAME == args.filter
        }
    }
    if args.json:
      if args.category in ("probe", "probes"):
        data = data["probes"]
      elif args.category in ("benchmark", "benchmarks"):
        data = data["benchmarks"]
      else:
        assert args.category == "all"
      print(json.dumps(data, indent=2))
      return
    # Create tabular format
    if args.category in ("all", "benchmarks"):
      table: List[List[Optional[str]]] = [["Benchmark", "Property", "Value"]]
      for benchmark_name, values in data["benchmarks"].items():
        table.append([benchmark_name, ])
        for name, value in values.items():
          if isinstance(value, (tuple, list)):
            value = "\n".join(value)
          elif isinstance(value, dict):
            value = tabulate(value.items(), tablefmt="plain")
          table.append([None, name, value])
      if len(table) > 1:
        print(tabulate(table, tablefmt="grid"))

    if args.category in ("all", "probes"):
      table = [["Probe", "Help"]]
      for probe_name, probe_desc in data["probes"].items():
        table.append([probe_name, probe_desc])
      if len(table) > 1:
        print(tabulate(table, tablefmt="grid"))

  def _setup_benchmark_subparser(self,
                                 benchmark_cls: Type[cb.benchmarks.Benchmark],
                                 aliases: Sequence[str]):
    subparser = benchmark_cls.add_cli_parser(self.subparsers, aliases)
    self.RUNNER_CLS.add_cli_parser(subparser)
    assert isinstance(subparser, argparse.ArgumentParser), (
        f"Benchmark class {benchmark_cls}.add_cli_parser did not return "
        f"an ArgumentParser: {subparser}")
    self._subparsers[benchmark_cls] = subparser

    env_group = subparser.add_argument_group("Runner Environment Settings", "")
    env_settings_group = env_group.add_mutually_exclusive_group()
    env_settings_group.add_argument(
        "--env",
        type=inline_env_config,
        help="Set default runner environment settings. "
        f"Possible values: {', '.join(cb.env.HostEnvironment.CONFIGS)}"
        "or an inline hjson configuration (see --env-config). "
        "Mutually exclusive with --env-config")
    env_settings_group.add_argument(
        "--env-config",
        type=env_config_file,
        help="Path to an env.config.hjson file that specifies detailed "
        "runner environment settings and requirements. "
        "See config/env.config.hjson for more details."
        "Mutually exclusive with --env")

    env_check_mode_group = env_group.add_mutually_exclusive_group()
    env_check_mode_group.add_argument(
        "--env-validation",
        default="prompt",
        choices=[mode.value for mode in cb.env.ValidationMode],
        help=textwrap.dedent("""
          Set how runner env is validated (see als --env-config/--env):
            throw:  Strict mode, throw and abort on env issues,
            prompt: Prompt to accept potential env issues,
            warn:   Only display a warning for env issues,
            skip:   Don't perform any env validation". 
          """))
    env_group.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Don't run any browsers or probes")

    browser_group = subparser.add_mutually_exclusive_group()
    browser_group.add_argument(
        "--browser",
        action="append",
        default=[],
        help="Browser binary. Use this to test a simple browser variant. "
        "Use [chrome, stable, dev, canary, safari] "
        "for system default browsers or a full path. "
        "Repeat for adding multiple browsers. "
        "Defaults to 'chrome-stable'. "
        "Use --browser=chrome-M107 to download the latest milestone, "
        "--browser=chrome-100.0.4896.168 to download a specific version "
        "(macos and googlers only)."
        "Cannot be used with --browser-config")
    browser_group.add_argument(
        "--browser-config",
        type=existing_file_type,
        help="Browser configuration.json file. "
        "Use this to run multiple browsers and/or multiple flag configurations."
        "See config/browser.config.example.hjson on how to set up a complex "
        "configuration file. "
        "Cannot be used together with --browser.")

    probe_group = subparser.add_mutually_exclusive_group()
    probe_group.add_argument(
        "--probe",
        action="append",
        default=[],
        help="Enable general purpose probes to measure data on all cb.stories. "
        "This argument can be specified multiple times to add more probes. "
        "Cannot be used together with --probe-config."
        f"\n\nChoices: {', '.join(ProbeConfig.LOOKUP.keys())}")
    probe_group.add_argument(
        "--probe-config",
        type=existing_file_type,
        help="Browser configuration.json file. "
        "Use this config file to specify more complex Probe settings."
        "See config/probe.config.example.hjson on how to set up a complex "
        "configuration file. "
        "Cannot be used together with --probe.")

    chrome_args = subparser.add_argument_group(
        "Chrome-forwarded Options",
        "For convenience these arguments are directly are forwarded "
        "directly to chrome. Any other browser option can be passed "
        "after the '--' arguments separator.")
    chrome_args.add_argument("--js-flags", dest="js_flags")

    doc_str = "See chrome's base/feature_list.h source file for more details"
    chrome_args.add_argument(
        "--enable-features",
        help="Comma-separated list of enabled chrome features. " + doc_str,
        default="")
    chrome_args.add_argument(
        "--disable-features",
        help="Command-separated list of disabled chrome features. " + doc_str,
        default="")
    subparser.set_defaults(
        subcommand=self.benchmark_subcommand, benchmark_cls=benchmark_cls)
    self._add_verbosity_argument(subparser)
    subparser.add_argument("other_browser_args", nargs="*")

  def benchmark_subcommand(self, args: argparse.Namespace):
    self._benchmark_subcommand_helper(args)
    benchmark = self._get_benchmark(args)
    runner = None
    try:
      with tempfile.TemporaryDirectory(prefix="crossbench") as tmp_dirname:
        if args.dry_run:
          args.out_dir = pathlib.Path(tmp_dirname) / "results"
        args.browser = self._get_browsers(args)
        probes = self._get_probes(args)
        env_config = self._get_env_config(args)
        env_validation_mode = self._get_env_validation_mode(args)
        runner = self._get_runner(args, benchmark, env_config,
                                  env_validation_mode)
        for probe in probes:
          runner.attach_probe(probe, matching_browser_only=True)

        self._run_benchmark(args, runner)
    except KeyboardInterrupt:
      sys.exit(2)
    except Exception as e:  # pylint: disable=broad-except
      if args.throw:
        raise
      self._log_benchmark_subcommand_failure(benchmark, runner, e)
      sys.exit(3)

  def _benchmark_subcommand_helper(self, args):
    """Handle common subcommand mistakes that are not easily implementable
    with argparse.
    run: => just run the benchmark
    help => use --help
    describe => use describe benchmark NAME
    """
    if not args.other_browser_args:
      return
    maybe_command = args.other_browser_args[0]
    if maybe_command == "run":
      args.other_browser_args.pop()
      return
    if maybe_command == "help":
      logging.error("Please use --help")
      self._subparsers[args.benchmark_cls].print_help()
      sys.exit(0)
    if maybe_command == "describe":
      logging.warning("Please use `describe benchmark %s`",
                      args.benchmark_cls.NAME)
      # Patch args to simulate: describe benchmark BENCHMARK_NAME
      args.category = "benchmarks"
      args.filter = args.benchmark_cls.NAME
      args.json = False
      self.describe_subcommand(args)
      sys.exit(0)

  def _log_benchmark_subcommand_failure(self, benchmark,
                                        runner: Optional[cb.runner.Runner],
                                        e: Exception):
    logging.debug(e)
    logging.error("")
    logging.error("#" * 80)
    logging.error("SUBCOMMAND UNSUCCESSFUL got %s:", e.__class__.__name__)
    logging.error("-" * 80)
    logging.error(e)
    logging.error("-" * 80)
    logging.error("Running '%s' was not successful:", benchmark.NAME)
    logging.error("- Check run results.json for detailed backtraces")
    logging.error("- Use --throw to throw on the first logged exception")
    logging.error("- Use -vv for detailed logging")
    if runner and runner.runs:
      self._log_runner_debug_hints(runner)
    logging.error("#" * 80)

  def _log_runner_debug_hints(self, runner: cb.runner.Runner):
    failed_runs = [run for run in runner.runs if not run.is_success]
    if not failed_runs:
      return
    failed_run = failed_runs[0]
    logging.error("- Check log outputs (first out of %d failed runs): %s",
                  len(failed_runs), failed_run.out_dir)
    for log_file in failed_run.out_dir.glob("*.log"):
      try:
        log_file = log_file.relative_to(pathlib.Path.cwd())
      finally:
        pass
      logging.error("  - %s", log_file)

  def _run_benchmark(self, args: argparse.Namespace, runner: cb.runner.Runner):
    try:
      runner.run(is_dry_run=args.dry_run)
      logging.info("")
      logging.info("=" * 80)
      logging.info("RESULTS: %s", runner.out_dir)
      logging.info("=" * 80)
    except:  # pylint disable=broad-except
      logging.info("=" * 80)
      logging.info("RESULTS (maybe incomplete/broken): %s", runner.out_dir)
      logging.info("=" * 80)
      raise

  def _get_browsers(self,
                    args: argparse.Namespace) -> Sequence[cb.browsers.Browser]:
    args.browser_config = BrowserConfig.from_cli_args(args)
    return args.browser_config.variants

  def _get_probes(self, args: argparse.Namespace) -> Sequence[cb.probes.Probe]:
    args.probe_config = ProbeConfig.from_cli_args(args)
    return args.probe_config.probes

  def _get_benchmark(self, args: argparse.Namespace) -> cb.benchmarks.Benchmark:
    benchmark_cls = self._get_benchmark_cls(args)
    assert issubclass(benchmark_cls, cb.benchmarks.Benchmark), (
        f"benchmark_cls={benchmark_cls} is not subclass of Runner")
    return benchmark_cls.from_cli_args(args)

  def _get_benchmark_cls(self, args: argparse.Namespace
                        ) -> Type[cb.benchmarks.Benchmark]:
    return args.benchmark_cls

  def _get_env_validation_mode(self, args) -> cb.env.ValidationMode:
    return cb.env.ValidationMode[args.env_validation.upper()]

  def _get_env_config(self, args) -> cb.env.HostEnvironmentConfig:
    if args.env:
      return args.env
    if args.env_config:
      return args.env_config
    return cb.env.HostEnvironmentConfig()

  def _get_runner(self, args: argparse.Namespace, benchmark,
                  env_config: cb.env.HostEnvironmentConfig,
                  env_validation_mode: cb.env.ValidationMode
                 ) -> cb.runner.Runner:
    runner_kwargs = self.RUNNER_CLS.kwargs_from_cli(args)
    return self.RUNNER_CLS(
        benchmark=benchmark,
        env_config=env_config,
        env_validation_mode=env_validation_mode,
        **runner_kwargs)

  def run(self, argv):
    args: argparse.Namespace = self.parser.parse_args(argv)
    self._initialize_logging(args)
    args.subcommand(args)

  def _initialize_logging(self, args: argparse.Namespace):
    logging.getLogger().setLevel(logging.INFO)
    console_handler = logging.StreamHandler()
    if args.verbosity == 0:
      console_handler.setLevel(logging.INFO)
    elif args.verbosity >= 1:
      console_handler.setLevel(logging.DEBUG)
      logging.getLogger().setLevel(logging.DEBUG)
    console_handler.addFilter(logging.Filter("root"))
    if args.color:
      console_handler.setFormatter(cb.helper.ColoredLogFormatter())
    logging.getLogger().addHandler(console_handler)
