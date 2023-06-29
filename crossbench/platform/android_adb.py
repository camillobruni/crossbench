# Copyright 2023 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from __future__ import annotations

import logging
import pathlib
import re
from functools import lru_cache
import subprocess
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from .platform import MachineArch, Platform
from .posix import PosixPlatform


class Adb:

  _serial_id: str
  _device_info: str

  def __init__(self,
               host_platform: Platform,
               device_identifier: Optional[str] = None) -> None:
    self._host_platform = host_platform
    self.start_server()
    self._serial_id, self._device_info = self._find_serial_id(device_identifier)
    logging.debug("ADB Selected device: %s %s", self._serial_id,
                  self._device_info)
    assert self._serial_id

  def _find_serial_id(self,
                      device_identifier: Optional[str] = None
                     ) -> Tuple[str, str]:
    devices = self.devices()
    if not devices:
      raise ValueError("adb could not find any attached devices."
                       "Connect your device and use 'adb devices' to list all.")
    if device_identifier is None:
      if len(devices) != 1:
        raise ValueError(
            f"Too many adb devices attached, please specify one of: {devices}")
      device_identifier = list(devices.keys())[0]
    assert device_identifier, f"Invalid device identifier: {device_identifier}"
    if device_identifier in devices:
      return device_identifier, devices[device_identifier]
    matches = []
    under_name = device_identifier.replace(" ", "_")
    for key, value in devices.items():
      if device_identifier in value or under_name in value:
        matches.append(key)
    if not matches:
      raise ValueError(
          f"Could not find adb device matching: '{device_identifier}'")
    if len(matches) > 1:
      raise ValueError(
          f"Found {len(matches)} adb devices matching: '{device_identifier}'.\n"
          f"Choices: {matches}")
    return matches[0], devices[matches[0]]

  def __str__(self) -> str:
    return f"adb({self._serial_id})"

  @property
  def serial_id(self) -> str:
    return self._serial_id

  @property
  def device_info(self) -> str:
    return self._device_info

  def _adb(self,
           *args: Union[str, pathlib.Path],
           shell: bool = False,
           capture_output: bool = False,
           stdout=None,
           stderr=None,
           stdin=None,
           env: Optional[Mapping[str, str]] = None,
           quiet: bool = False,
           check: bool = True,
           use_serial_id: bool = True) -> subprocess.CompletedProcess:
    if use_serial_id:
      adb_cmd = ["adb", "-s", self._serial_id]
    else:
      adb_cmd = ["adb"]
    adb_cmd.extend(args)
    return self._host_platform.sh(
        *adb_cmd,
        shell=shell,
        capture_output=capture_output,
        stdout=stdout,
        stderr=stderr,
        stdin=stdin,
        env=env,
        quiet=quiet,
        check=check)

  def _adb_stdout(self,
                  *args: Union[str, pathlib.Path],
                  quiet: bool = False,
                  encoding: str = "utf-8",
                  use_serial_id: bool = True) -> str:
    if use_serial_id:
      adb_cmd = ["adb", "-s", self._serial_id]
    else:
      adb_cmd = ["adb"]
    adb_cmd.extend(args)
    return self._host_platform.sh_stdout(
        *adb_cmd, quiet=quiet, encoding=encoding)

  def shell_stdout(self,
                   *args: Union[str, pathlib.Path],
                   quiet: bool = False,
                   encoding: str = "utf-8") -> str:
    # -e: choose escape character, or "none"; default '~'
    # -n: don't read from stdin
    # -T: disable pty allocation
    # -t: allocate a pty if on a tty (-tt: force pty allocation)
    # -x: disable remote exit codes and stdout/stderr separation
    return self._adb_stdout("shell", *args, quiet=quiet, encoding=encoding)

  def shell(self,
            *args: Union[str, pathlib.Path],
            shell: bool = False,
            capture_output: bool = False,
            stdout=None,
            stderr=None,
            stdin=None,
            env: Optional[Mapping[str, str]] = None,
            quiet: bool = False,
            check: bool = True) -> subprocess.CompletedProcess:
    # See shell_stdout for more `adb shell` options.
    adb_cmd = ["shell", *args]
    return self._adb(
        *adb_cmd,
        shell=shell,
        capture_output=capture_output,
        stdout=stdout,
        stderr=stderr,
        stdin=stdin,
        env=env,
        quiet=quiet,
        check=check)

  def start_server(self) -> None:
    self._adb_stdout("start-server", use_serial_id=False)

  def stop_server(self) -> None:
    self.kill_server()

  def kill_server(self) -> None:
    self._adb_stdout("kill-server", use_serial_id=False)

  def devices(self) -> Dict[str, str]:
    raw_lines = self._adb_stdout(
        "devices", "-l", use_serial_id=False).strip().split("\n")[1:]
    result = {}
    for line in raw_lines:
      serial_id, details = line.split(" ", maxsplit=1)
      result[serial_id.strip()] = details.strip()
    return result

  def pull(self, device_src_path: pathlib.Path,
           local_dest_path: pathlib.Path) -> None:
    self._adb("pull", device_src_path, local_dest_path)

  def cmd(self,
          *args: str,
          quiet: bool = False,
          encoding: str = "utf-8") -> str:
    cmd = ["cmd", *args]
    return self.shell_stdout(*cmd, quiet=quiet, encoding=encoding)

  def dumpsys(self,
              *args: str,
              quiet: bool = False,
              encoding: str = "utf-8") -> str:
    cmd = ["dumpsys", *args]
    return self.shell_stdout(*cmd, quiet=quiet, encoding=encoding)

  def getprop(self,
              *args: str,
              quiet: bool = False,
              encoding: str = "utf-8") -> str:
    cmd = ["getprop", *args]
    return self.shell_stdout(*cmd, quiet=quiet, encoding=encoding).strip()

  def services(self, quiet: bool = False, encoding: str = "utf-8") -> List[str]:
    lines = list(
        self.cmd("-l", quiet=quiet, encoding=encoding).strip().split("\n"))
    lines = lines[1:]
    lines.sort()
    return [line.strip() for line in lines]

  def packages(self, quiet: bool = False, encoding: str = "utf-8") -> List[str]:
    # adb shell cmd package list packages
    raw_list = self.cmd(
        "package", "list", "packages", quiet=quiet,
        encoding=encoding).strip().split("\n")
    packages = [package.split(":", maxsplit=2)[1] for package in raw_list]
    packages.sort()
    return packages


class AndroidAdbPlatform(PosixPlatform):

  def __init__(self,
               host_platform: Platform,
               device_identifier: Optional[str] = None) -> None:
    super().__init__()
    self._host_platform = host_platform
    assert not host_platform.is_remote, (
        "adb on remote platform is not supported yet")
    self._adb = Adb(host_platform, device_identifier)

  @property
  def is_remote(self) -> bool:
    return True

  @property
  def is_android(self) -> bool:
    return True

  @property
  def name(self) -> str:
    return "android"

  @property
  def host_platform(self) -> Platform:
    return self._host_platform

  @property
  @lru_cache
  def version(self) -> str:
    return self.adb.getprop("ro.build.version.release")

  @property
  @lru_cache
  def device(self) -> str:
    return self.adb.getprop("ro.product.model")

  @property
  @lru_cache
  def cpu(self) -> str:
    variant = self.adb.getprop("dalvik.vm.isa.arm.variant")
    platform = self.adb.getprop("ro.board.platform")
    try:
      # TODO: add file_contents helper on platform
      _, max_core = self.cat("/sys/devices/system/cpu/possible").strip().split(
          "-", maxsplit=1)
      cores = int(max_core) + 1
      return f"{variant} {platform} {cores} cores"
    except Exception:
      return f"{variant} {platform}"

  @property
  def adb(self) -> Adb:
    return self._adb

  _MACHINE_ARCH_LOOKUP = {
      "arm64-v8a": MachineArch.ARM_64,
      "armeabi-v7a": MachineArch.ARM_32,
      "x86": MachineArch.IA32,
      "x86_64": MachineArch.X64,
  }

  @property
  @lru_cache
  def machine(self) -> MachineArch:
    cpu_abi = self.adb.getprop("ro.product.cpu.abi")
    arch = self._MACHINE_ARCH_LOOKUP.get(cpu_abi, None)
    if arch is None:
      raise ValueError("Unknown android CPU ABI: {cpu_abi}")
    return arch

  def app_path_to_package(self, app_path: pathlib.Path) -> str:
    if len(app_path.parts) > 1:
      raise ValueError(f"Invalid android package name: '{app_path}'")
    package: str = app_path.parts[0]
    packages = self.adb.packages()
    if package not in packages:
      raise ValueError(f"Package '{package}' is not installed on {self._adb}")
    return package

  def search_binary(self, app_path: pathlib.Path) -> Optional[pathlib.Path]:
    raise NotImplementedError()

  def search_app(self, app_path: pathlib.Path) -> Optional[pathlib.Path]:
    raise NotImplementedError()

  _VERSION_NAME_RE = re.compile(r"versionName=(?P<version>.+)")

  def app_version(self, app_or_bin_path: pathlib.Path) -> str:
    # adb shell dumpsys package com.chrome.canary | grep versionName -C2
    package = self.app_path_to_package(app_or_bin_path)
    package_info = self.adb.dumpsys("package", str(package))
    match_result = self._VERSION_NAME_RE.search(package_info)
    if match_result is None:
      raise ValueError(
          f"Could not find version for '{package}': {package_info}")
    return match_result.group('version')

  def process_children(self,
                       parent_pid: int,
                       recursive: bool = False) -> List[Dict[str, Any]]:
    # TODO: implement
    return []

  def foreground_process(self) -> Optional[Dict[str, Any]]:
    # adb shell dumpsys activity activities
    # TODO: implement
    return None

  def get_relative_cpu_speed(self) -> float:
    # TODO figure out
    return 1.0

  _GETPROP_RE = re.compile(r"^\[(?P<key>[^\]]+)\]: \[(?P<value>[^\]]+)\]$")

  def system_details(self) -> Dict[str, Any]:
    details = super().system_details()
    properties: Dict[str, str] = {}
    for line in self.adb.shell_stdout("getprop").strip().split("\n"):
      result = self._GETPROP_RE.fullmatch(line)
      if result:
        properties[result.group("key")] = result.group("value")
    details["android"] = properties
    return details

  def check_autobrightness(self) -> bool:
    # adb shell dumpsys display
    # TODO: implement.
    return True

  def get_main_display_brightness(self) -> int:
    # adb shell dumpsys display
    # TODO: implement.
    return 1

  @property
  def default_tmp_dir(self) -> pathlib.Path:
    return pathlib.Path("/data/local/tmp/")

  def sh(self,
         *args: Union[str, pathlib.Path],
         shell: bool = False,
         capture_output: bool = False,
         stdout=None,
         stderr=None,
         stdin=None,
         env: Optional[Mapping[str, str]] = None,
         quiet: bool = False,
         check: bool = False) -> subprocess.CompletedProcess:
    return self.adb.shell(
        *args,
        shell=shell,
        capture_output=capture_output,
        stdout=stdout,
        stderr=stderr,
        stdin=stdin,
        env=env,
        quiet=quiet,
        check=check)

  def rsync(self, from_path: pathlib.Path,
            to_path: pathlib.Path) -> pathlib.Path:
    assert self.exists(
        from_path), f"Source file '{from_path}' does not exist on {self}"
    to_path.parent.mkdir(parents=True, exist_ok=True)
    self.adb.pull(from_path, to_path)
    return to_path
