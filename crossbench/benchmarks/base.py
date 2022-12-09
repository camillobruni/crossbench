# Copyright 2022 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from __future__ import annotations

import abc
import argparse
import logging
import re
from typing import TYPE_CHECKING, Any, Dict, Generic, List, Sequence, Type, TypeVar, cast

import crossbench
import crossbench.stories
if TYPE_CHECKING:
  from crossbench.runner import Runner

# TODO fix imports
cb = crossbench


class Benchmark(abc.ABC):
  NAME: str = ""
  DEFAULT_STORY_CLS: Type[cb.stories.Story] = cb.stories.Story

  @classmethod
  def cli_help(cls) -> str:
    assert cls.__doc__, (f"Benchmark class {cls} must provide a doc string.")
    # Return the first non-empty line
    return cls.__doc__.strip().split("\n")[0]

  @classmethod
  def cli_description(cls) -> str:
    assert cls.__doc__
    return cls.__doc__.strip()

  @classmethod
  def cli_epilog(cls) -> str:
    return ""

  @classmethod
  def add_cli_parser(cls, subparsers,
                     aliases: Sequence[str] = ()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        cls.NAME,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        help=cls.cli_help(),
        description=cls.cli_description(),
        epilog=cls.cli_epilog(),
        aliases=aliases)
    return parser

  @classmethod
  def describe(cls) -> Dict[str, Any]:
    assert cls.__doc__
    return {
        "name": cls.NAME,
        "description": cls.cli_description(),
        "stories": [],
        "probes-default": {
            probe_cls.NAME: probe_cls.__doc__.strip()
            for probe_cls in cls.DEFAULT_STORY_CLS.PROBES
        }
    }

  @classmethod
  def kwargs_from_cli(cls, args) -> dict:
    del args
    return {}

  @classmethod
  def from_cli_args(cls, args) -> Benchmark:
    kwargs = cls.kwargs_from_cli(args)
    return cls(**kwargs)

  def __init__(self, stories: Sequence[cb.stories.Story]):
    assert self.NAME is not None, f"{self} has no .NAME property"
    assert self.DEFAULT_STORY_CLS != cb.stories.Story, (
        f"{self} has no .DEFAULT_STORY_CLS property")
    self.stories: List[cb.stories.Story] = self._validate_stories(stories)

  def _validate_stories(self, stories: Sequence[cb.stories.Story]
                       ) -> List[cb.stories.Story]:
    assert stories, "No stories provided"
    for story in stories:
      assert isinstance(story, self.DEFAULT_STORY_CLS), (
          f"story={story} should be a subclass/the same "
          f"class as {self.DEFAULT_STORY_CLS}")
    first_story = stories[0]
    expected_probes_cls_list = first_story.PROBES
    for story in stories:
      assert story.PROBES == expected_probes_cls_list, (
          f"story={story} has different PROBES than {first_story}")
    return list(stories)

  def setup(self, runner: Runner):
    del runner


StoryT = TypeVar("StoryT", bound=cb.stories.Story)


class StoryFilter(Generic[StoryT], metaclass=abc.ABCMeta):

  @classmethod
  def kwargs_from_cli(cls, args) -> Dict[str, Any]:
    return {"patterns": args.stories.split(",")}

  @classmethod
  def from_cli_args(cls, story_cls: Type[StoryT], args):
    kwargs = cls.kwargs_from_cli(args)
    return cls(story_cls, **kwargs)

  def __init__(self, story_cls: Type[StoryT], patterns: Sequence[str]):
    self.story_cls = story_cls
    assert issubclass(story_cls, cb.stories.Story), (
        f"Subclass of {cb.stories.Story} expected, found {story_cls}")
    # Using order-preserving dict instead of set
    self._known_names: Dict[str, None] = dict.fromkeys(
        story_cls.all_story_names())
    self.stories: Sequence[StoryT] = []
    self.process_all(patterns)
    self.stories = self.create_stories()

  @abc.abstractmethod
  def process_all(self, patterns: Sequence[str]):
    pass

  @abc.abstractmethod
  def create_stories(self) -> Sequence[StoryT]:
    pass


class SubStoryBenchmark(Benchmark, metaclass=abc.ABCMeta):
  STORY_FILTER_CLS: Type[StoryFilter] = StoryFilter

  @classmethod
  def add_cli_parser(cls, subparsers,
                     aliases: Sequence[str] = ()) -> argparse.ArgumentParser:
    parser = super().add_cli_parser(subparsers, aliases)
    parser.add_argument(
        "--stories",
        "--story",
        dest="stories",
        default="default",
        help="Comma-separated list of story names. "
        "Use 'all' for selecting all available stories. "
        "Use 'default' for the standard selection of stories.")
    is_combined_group = parser.add_mutually_exclusive_group()
    is_combined_group.add_argument(
        "--combined",
        dest="separate",
        default=False,
        action="store_false",
        help="Run each story in the same session. (default)")
    is_combined_group.add_argument(
        "--separate",
        action="store_true",
        help="Run each story in a fresh browser.")
    return parser

  @classmethod
  def cli_description(cls) -> str:
    desc = super().cli_description()
    desc += "\n\n"
    desc += ("Stories (alternatively use 'the describe benchmark "
             f"{cls.NAME}' command):\n")
    desc += ", ".join(cls.all_story_names())
    desc += "\n\n"
    desc += "Filtering (for --stories): "
    assert cls.STORY_FILTER_CLS.__doc__
    desc += cls.STORY_FILTER_CLS.__doc__.strip()

    return desc

  @classmethod
  def kwargs_from_cli(cls, args) -> Dict[str, Any]:
    kwargs = super().kwargs_from_cli(args)
    kwargs["stories"] = cls.stories_from_cli_args(args)
    return kwargs

  @classmethod
  def stories_from_cli_args(cls, args) -> Sequence[cb.stories.Story]:
    return cls.STORY_FILTER_CLS.from_cli_args(cls.DEFAULT_STORY_CLS,
                                              args).stories

  @classmethod
  def describe(cls) -> Dict[str, Any]:
    data = super().describe()
    data["stories"] = cls.all_story_names()
    return data

  @classmethod
  def all_story_names(cls) -> Sequence[str]:
    return sorted(cls.DEFAULT_STORY_CLS.all_story_names())


class PressBenchmarkStoryFilter(StoryFilter[cb.stories.PressBenchmarkStory]):
  """
  Filter stories by name or regexp.

  Syntax:
    "all"     Include all stories (defaults to story_names).
    "name"    Include story with the given name.
    "-name"   Exclude story with the given name'
    "foo.*"   Include stories whose name matches the regexp.
    "-foo.*"  Exclude stories whose name matches the regexp.

  These patterns can be combined:
    [".*", "-foo", "-bar"] Includes all except the "foo" and "bar" story
  """

  @classmethod
  def kwargs_from_cli(cls, args):
    kwargs = super().kwargs_from_cli(args)
    kwargs["separate"] = args.separate
    kwargs["is_live"] = args.is_live
    return kwargs

  def __init__(self,
               story_cls: Type[cb.stories.PressBenchmarkStory],
               patterns: Sequence[str],
               separate: bool = False,
               is_live: bool = False):
    self.separate = separate
    self.is_live: bool = is_live
    # Using dict instead as ordered set
    self._selected_names: Dict[str, None] = {}
    super().__init__(story_cls, patterns)
    assert issubclass(self.story_cls, cb.stories.PressBenchmarkStory)
    for name in self._known_names:
      assert name, "Invalid empty story name"
      assert not name.startswith("-"), (
          f"Known story names cannot start with '-', but got {name}.")
      assert not name == "all", "Known story name cannot match 'all'."

  def process_all(self, patterns: Sequence[str]):
    if not isinstance(patterns, (list, tuple)):
      raise ValueError("Expected Sequence of story name or patterns "
                       f"but got '{type(patterns)}'.")
    for pattern in patterns:
      self.process_pattern(pattern)

  def process_pattern(self, pattern: str):
    if pattern.startswith("-"):
      self.remove(pattern[1:])
    else:
      self.add(pattern)

  def add(self, pattern: str):
    self._check_processed_pattern(pattern)
    regexp = self._pattern_to_regexp(pattern)
    self._add_matching(regexp, pattern)

  def remove(self, pattern: str):
    self._check_processed_pattern(pattern)
    regexp = self._pattern_to_regexp(pattern)
    self._remove_matching(regexp, pattern)

  def _pattern_to_regexp(self, pattern) -> re.Pattern:
    if pattern == "all":
      return re.compile(".*")
    if pattern == "default":
      default_story_names = self.story_cls.default_story_names()
      if default_story_names == self.story_cls.all_story_names():
        return re.compile(".*")
      joined_names = "|".join(re.escape(name) for name in default_story_names)
      return re.compile(f"^({joined_names})$")
    if pattern in self._known_names:
      return re.compile(re.escape(pattern))
    return re.compile(pattern)

  def _check_processed_pattern(self, pattern: str):
    if not pattern:
      raise ValueError("Empty pattern is not allowed")
    if pattern == "-":
      raise ValueError(f"Empty remove pattern not allowed: '{pattern}'")
    if pattern[0] == "-":
      raise ValueError(f"Unprocessed negative pattern not allowed: '{pattern}'")

  def _add_matching(self, regexp: re.Pattern, original_pattern: str):
    substories = self._regexp_match(regexp, original_pattern)
    self._selected_names.update(dict.fromkeys(substories))

  def _remove_matching(self, regexp: re.Pattern, original_pattern: str):
    substories = self._regexp_match(regexp, original_pattern)
    for substory in substories:
      try:
        del self._selected_names[substory]
      except KeyError as e:
        raise ValueError(
            "Removing Story failed: "
            f"name='{substory}' extracted by pattern='{original_pattern}'"
            "is not in the filtered story list") from e

  def _regexp_match(self, regexp: re.Pattern,
                    original_pattern: str) -> List[str]:
    substories = [
        substory for substory in self._known_names if regexp.fullmatch(substory)
    ]
    if not substories:
      raise ValueError(f"'{original_pattern}' didn't match any stories.")
    if len(substories) == len(self._known_names) and self._selected_names:
      raise ValueError(f"'{original_pattern}' matched all and overrode all"
                       "previously filtered story names.")
    return substories

  def create_stories(self) -> Sequence[StoryT]:
    logging.info("SELECTED STORIES: %s",
                 str(list(map(str, self._selected_names))))
    names = list(self._selected_names.keys())
    return self.story_cls.from_names(
        names, separate=self.separate, is_live=self.is_live)


class PressBenchmark(SubStoryBenchmark):
  STORY_FILTER_CLS = PressBenchmarkStoryFilter
  DEFAULT_STORY_CLS: Type[
      cb.stories.PressBenchmarkStory] = cb.stories.PressBenchmarkStory

  @classmethod
  def add_cli_parser(cls, subparsers,
                     aliases: Sequence[str] = ()) -> argparse.ArgumentParser:
    parser = super().add_cli_parser(subparsers, aliases)
    is_live_group = parser.add_mutually_exclusive_group()
    is_live_group.add_argument(
        "--live",
        default=True,
        dest="is_live",
        action="store_true",
        help="Use live/online benchmark url.")
    is_live_group.add_argument(
        "--local",
        dest="is_live",
        action="store_false",
        help="Use locally hosted benchmark url.")
    return parser

  @classmethod
  def kwargs_from_cli(cls, args) -> Dict[str, Any]:
    kwargs = super().kwargs_from_cli(args)
    kwargs["is_live"] = args.is_live
    return kwargs

  @classmethod
  def describe(cls) -> dict:
    data = super().describe()
    assert issubclass(cls.DEFAULT_STORY_CLS, cb.stories.PressBenchmarkStory)
    data["url"] = cls.DEFAULT_STORY_CLS.URL
    data["url-local"] = cls.DEFAULT_STORY_CLS.URL_LOCAL
    return data

  def __init__(self, stories: Sequence[cb.stories.Story], is_live: bool = True):
    super().__init__(stories)
    self.is_live: bool = is_live

  def setup(self, runner: Runner):
    super().setup(runner)
    self.validate_url(runner)

  def validate_url(self, runner: Runner):
    first_story = cast(cb.stories.PressBenchmarkStory, self.stories[0])
    url = first_story.url
    if not url:
      raise ValueError("Invalid empty url")
    if runner.env.validate_url(url):
      return
    if self.is_live:
      raise Exception(f"Could not reach live benchmark URL: '{url}'. "
                      f"Please make sure you're connected to the internet.")
    raise Exception(f"Could not reach local benchmark URL: '{url}'. "
                    f"Please make sure your local web server is running")
