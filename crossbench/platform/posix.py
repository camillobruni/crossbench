# Copyright 2023 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from __future__ import annotations

import abc
import pathlib
from functools import lru_cache
from typing import Iterator, List, Optional, Union

from .platform import Environ, Platform


class PosixPlatform(Platform, metaclass=abc.ABCMeta):
  # pylint: disable=locally-disabled, redefined-builtin

  def app_version(self, app_or_bin: pathlib.Path) -> str:
    assert app_or_bin.exists(), f"Binary {app_or_bin} does not exist."
    return self.sh_stdout(app_or_bin, "--version")

  @property
  @lru_cache
  def version(self) -> str:
    return self.sh_stdout("uname", "-r").strip()

  def cat(self, file: Union[str, pathlib.Path], encoding: str = "utf-8") -> str:
    if self.is_remote:
      return self.sh_stdout("cat", file, encoding=encoding)
    return super().cat(file, encoding)

  def rm(self, path: Union[str, pathlib.Path], dir: bool = False) -> None:
    if self.is_remote:
      if dir:
        self.sh("rm", "-rf", path)
      else:
        self.sh("rm", path)
    else:
      super().rm(path, dir)

  def mkdir(self, path: Union[str, pathlib.Path]) -> None:
    if self.is_remote:
      self.sh("mkdir", "-p", path)
    else:
      super().mkdir(path)

  def mkdtemp(self,
              prefix: Optional[str] = None,
              dir: Optional[Union[str, pathlib.Path]] = None) -> pathlib.Path:
    if self.is_remote:
      return self._mktemp_sh(is_dir=True, prefix=prefix, dir=dir)
    return super().mkdtemp(prefix, dir)

  def mktemp(self,
             prefix: Optional[str] = None,
             dir: Optional[Union[str, pathlib.Path]] = None) -> pathlib.Path:
    if self.is_remote:
      return self._mktemp_sh(is_dir=False, prefix=prefix, dir=dir)
    return super().mktemp(prefix, dir)

  def _mktemp_sh(self, is_dir: bool, prefix: Optional[str],
                 dir: Optional[Union[str, pathlib.Path]]) -> pathlib.Path:
    if not dir:
      dir = self.default_tmp_dir
    template = pathlib.Path(dir) / f"{prefix}.XXXXXXXXXXX"
    args: List[str] = ["mktemp"]
    if is_dir:
      args.append("-d")
    args.append(str(template))
    result = self.sh_stdout(*args)
    return pathlib.Path(result.strip())

  def exists(self, path: pathlib.Path) -> bool:
    if self.is_remote:
      return self.sh("[", "-e", path, "]", check=False).returncode == 0
    return super().exists(path)

  def is_file(self, path: pathlib.Path) -> bool:
    if self.is_remote:
      return self.sh("[", "-f", path, "]", check=False).returncode == 0
    return super().is_file(path)

  def is_dir(self, path: pathlib.Path) -> bool:
    if self.is_remote:
      return self.sh("[", "-d", path, "]", check=False).returncode == 0
    return super().is_dir(path)

  @property
  def environ(self) -> Environ:
    if self.is_remote:
      return RemotePosixEnviron(self)
    return super().environ


class RemotePosixEnviron(Environ):

  def __init__(self, platform: PosixPlatform) -> None:
    self._platform = platform
    self._environ = dict(
        line.split("=", maxsplit=1)
        for line in self._platform.sh_stdout("env").splitlines())

  def __getitem__(self, key: str) -> str:
    return self._environ.__getitem__(key)

  def __setitem__(self, key: str, item: str) -> None:
    raise NotImplementedError("Unsupported")

  def __delitem__(self, key: str) -> None:
    raise NotImplementedError("Unsupported")

  def __iter__(self) -> Iterator[str]:
    return self._environ.__iter__()

  def __len__(self) -> int:
    return self._environ.__len__()
