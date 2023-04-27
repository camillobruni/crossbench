# Copyright 2023 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from __future__ import annotations

import argparse
import contextlib
import json
import math
import pathlib
from typing import Any, Generator


def parse_path(str_value: str) -> pathlib.Path:
  try:
    path = pathlib.Path(str_value).expanduser()
  except RuntimeError as e:
    raise argparse.ArgumentTypeError(f"Invalid Path '{str_value}': {e}") from e
  if not path.exists():
    raise argparse.ArgumentTypeError(f"Path '{path}', does not exist.")
  return path


def parse_file_path(str_value: str) -> pathlib.Path:
  path = parse_path(str_value)
  if not path.is_file():
    raise argparse.ArgumentTypeError(f"Path '{path}', is not a file.")
  return path


def parse_dir_path(str_value: str) -> pathlib.Path:
  path = parse_path(str_value)
  if not path.is_dir():
    raise argparse.ArgumentTypeError(f"Path '{path}', is not a file.")
  return path


def parse_json_file_path(str_value: str) -> pathlib.Path:
  path = parse_file_path(str_value)
  with path.open(encoding="utf-8") as f:
    try:
      json.load(f)
    except ValueError as e:
      raise argparse.ArgumentTypeError(f"Invalid json file: {path}: {e}") from e
  return path


def parse_json_file(str_value: str) -> Any:
  path = parse_file_path(str_value)
  with path.open(encoding="utf-8") as f:
    return json.load(f)


def parse_positive_float(value: str) -> float:
  value_f = float(value)
  if not math.isfinite(value_f) or value_f < 0:
    raise argparse.ArgumentTypeError(
        f"Expected positive value but got: {value_f}")
  return value_f


def parse_positive_zero_int(value: str) -> int:
  positive_int = int(value)
  if positive_int < 0:
    raise argparse.ArgumentTypeError(f"Expected int >= 0, got: {positive_int}")
  return positive_int


def parse_positive_int(value: str) -> int:
  positive_int = int(value)
  if positive_int <= 0:
    raise argparse.ArgumentTypeError(f"Expected int > 0, but got: {value}")
  return positive_int


class CrossBenchArgumentError(argparse.ArgumentError):
  """Custom class that also prints the argument.help if available.
  """

  def __init__(self, argument: Any, message: str) -> None:
    self.help: str = ""
    super().__init__(argument, message)
    if self.argument_name:
      self.help = getattr(argument, "help", "")

  def __str__(self) -> str:
    formatted = super().__str__()
    if not self.help:
      return formatted
    return (f"argument error {self.argument_name}:\n\n"
            f"Help {self.argument_name}:\n{self.help}\n\n"
            f"{formatted}")


class LateArgumentError(argparse.ArgumentTypeError):
  """Signals argument parse errors after parser.parse_args().
  This is used to map errors back to the original argument, much like
  argparse.ArgumentError does internally. However, since this happens after
  the internal argument parsing we need this custom implementation to print
  more descriptive error messages.
  """

  def __init__(self, flag: str, message: str):
    super().__init__(message)
    self.flag = flag
    self.message = message


@contextlib.contextmanager
def late_argument_type_error_wrapper(flag: str) -> Generator[None, None, None]:
  """Converts raised ValueError and ArgumentTypeError to LateArgumentError
  that are associated with the given flag.
  """
  try:
    yield
  except (ValueError, argparse.ArgumentTypeError) as e:
    raise LateArgumentError(flag, str(e)) from e
