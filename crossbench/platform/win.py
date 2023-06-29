# Copyright 2023 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from __future__ import annotations

import pathlib
from typing import Optional, Union
import os

from .platform import Platform


class WinPlatform(Platform):
  SEARCH_PATHS = (
      pathlib.Path("."),
      pathlib.Path(os.path.expandvars("%ProgramFiles%")),
      pathlib.Path(os.path.expandvars("%ProgramFiles(x86)%")),
      pathlib.Path(os.path.expandvars("%APPDATA%")),
      pathlib.Path(os.path.expandvars("%LOCALAPPDATA%")),
  )

  @property
  def is_win(self) -> bool:
    return True

  @property
  def name(self) -> str:
    return "win"

  @property
  def device(self) -> str:
    # TODO: implement
    return ""

  @property
  def version(self) -> str:
    # TODO: implement
    return ""

  @property
  def cpu(self) -> str:
    # TODO: implement
    return ""

  def search_binary(self, app_or_bin: pathlib.Path) -> Optional[pathlib.Path]:
    if app_or_bin.suffix != ".exe":
      raise ValueError("Expected executable path with '.exe' suffix, "
                       f"but got: '{app_or_bin.name}'")
    for path in self.SEARCH_PATHS:
      # Recreate Path object for easier pyfakefs testing
      result_path = pathlib.Path(path) / app_or_bin
      if result_path.exists():
        return result_path
    return None

  def app_version(self, app_or_bin: pathlib.Path) -> str:
    assert app_or_bin.exists(), f"Binary {app_or_bin} does not exist."
    return self.sh_stdout(
        "powershell", "-command",
        f"(Get-Item '{app_or_bin}').VersionInfo.ProductVersion")