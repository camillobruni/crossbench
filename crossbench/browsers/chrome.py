# Copyright 2022 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from __future__ import annotations

import logging
import pathlib
import plistlib
import re
import shutil
import tempfile
from typing import TYPE_CHECKING, Final, List, Optional, Tuple

from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService

import crossbench
import crossbench.flags
from crossbench import helper
from crossbench.browsers import BROWSERS_CACHE
from crossbench.browsers.chromium import Chromium, ChromiumWebDriver

#TODO: fix imports
cb = crossbench

if TYPE_CHECKING:
  from selenium.webdriver.chromium.webdriver import ChromiumDriver

FlagsInitialDataType = cb.flags.Flags.InitialDataType


class Chrome(Chromium):

  @classmethod
  def default_path(cls) -> pathlib.Path:
    return cls.stable_path()

  @classmethod
  def stable_path(cls) -> pathlib.Path:
    return helper.search_app_or_executable(
        "Chrome Stable",
        macos=["Google Chrome.app"],
        linux=["google-chrome", "chrome"],
        win=["Google/Chrome/Application/chrome.exe"])

  @classmethod
  def beta_path(cls) -> pathlib.Path:
    return helper.search_app_or_executable(
        "Chrome Beta",
        macos=["Google Chrome Beta.app"],
        linux=["google-chrome-beta"],
        win=["Google/Chrome Beta/Application/chrome.exe"])

  @classmethod
  def dev_path(cls) -> pathlib.Path:
    return helper.search_app_or_executable(
        "Chrome Dev",
        macos=["Google Chrome Dev.app"],
        linux=["google-chrome-unstable"],
        win=["Google/Chrome Dev/Application/chrome.exe"])

  @classmethod
  def canary_path(cls) -> pathlib.Path:
    return helper.search_app_or_executable(
        "Chrome Canary",
        macos=["Google Chrome Canary.app"],
        win=["Google/Chrome SxS/Application/chrome.exe"])

  def __init__(self,
               label: str,
               path: pathlib.Path,
               js_flags: FlagsInitialDataType = None,
               flags: FlagsInitialDataType = None,
               cache_dir: Optional[pathlib.Path] = None,
               platform: Optional[helper.Platform] = None):
    super().__init__(
        label,
        path,
        js_flags,
        flags,
        cache_dir,
        type="chrome",
        platform=platform)


class ChromeWebDriver(ChromiumWebDriver):

  WEB_DRIVER_OPTIONS = ChromeOptions
  WEB_DRIVER_SERVICE = ChromeService

  def __init__(self,
               label: str,
               path: pathlib.Path,
               js_flags: FlagsInitialDataType = None,
               flags: FlagsInitialDataType = None,
               cache_dir: Optional[pathlib.Path] = None,
               driver_path: Optional[pathlib.Path] = None,
               platform: Optional[helper.Platform] = None):
    super().__init__(
        label,
        path,
        js_flags,
        flags,
        cache_dir,
        type="chrome",
        driver_path=driver_path,
        platform=platform)

  def _create_driver(self, options, service) -> ChromiumDriver:
    return webdriver.Chrome(  # pytype: disable=wrong-keyword-args 
        options=options,
        service=service)


class ChromeDownloader:

  VERSION_RE: Final = re.compile(r"^chrome-((m[0-9]+)|([0-9]+(\.[0-9]+){3}))$",
                                 re.I)
  ANY_MARKER: Final = 9999
  URL: Final = "gs://chrome-signed/desktop-5c0tCh/"

  @classmethod
  def load(cls, version_identifier: str) -> pathlib.Path:
    loader = cls(version_identifier)
    assert loader.path.exists(), "Could not download browser"
    return loader.path

  def __init__(self, version_identifier):
    self.version_identifier = ""
    self.platform = helper.platform
    self._pre_check()
    self.requested_version = (0, 0, 0, 0)
    self.requested_version_str = "0.0.0.0"
    self.requested_exact_version = True
    version_identifier = version_identifier.lower()
    self.path = self._parse_version(version_identifier)
    logging.info("-" * 80)
    if self.path.exists():
      cached_version = self._validate_cached()
      logging.info("CACHED BROWSER: %s %s", cached_version, self.path)
    else:
      logging.info("DOWNLOADING CHROME %s", version_identifier.upper())
      self._download()

  def _pre_check(self):
    # TODO: Add support for win/linux as well
    assert self.platform.is_macos, (
        "Downloading chrome versions is only supported on mac")
    assert not self.platform.is_remote, (
        "Browser download only supported on local machines")
    if not self.platform.which("gsutil"):
      raise ValueError(
          f"Cannot download chrome version {self.version_identifier}: "
          "please install gsutil.\n"
          "See https://cloud.google.com/storage/docs/gsutil_install")

  def _parse_version(self, version_identifier: str):
    match = self.VERSION_RE.match(version_identifier)
    assert match, (f"Invalid chrome version identifier: {version_identifier}")
    self.version_identifier = version_identifier = match[1]
    if version_identifier[0] == "m":
      self.requested_version = (int(version_identifier[1:]),)
      self.requested_version_str = f"M{self.requested_version[0]}"
      path = BROWSERS_CACHE / f"Chrome {self.requested_version_str}.app"
    else:
      self.requested_version = tuple(map(int,
                                         version_identifier.split(".")))[:4]
      self.requested_version_str = ".".join(map(str, self.requested_version))
      path = BROWSERS_CACHE / f"Chrome {self.requested_version_str}.app"
    # Normalize version numbers, use ANY_MARKER as "don't-care" value to find
    # the newest version
    while len(self.requested_version) < 4:
      self.requested_version += (self.ANY_MARKER,)
      self.requested_exact_version = False
    assert len(self.requested_version) == 4
    return path

  def _validate_cached(self) -> str:
    # "Google Chrome 107.0.5304.121" => "107.0.5304.121"
    cached_version_str = self.platform.app_version(self.path).split(" ")[-1]
    cached_version = tuple(map(int, cached_version_str.split(".")))
    if not self._version_matches(cached_version):
      raise ValueError(
          f"Previously downloaded browser at {self.path} might have been auto-updated.\n"
          "Please delete the old version and re-install/-download it.\n"
          f"Expected: {self.requested_version} Got: {cached_version}")
    return cached_version_str

  def _download(self):
    if self.platform.is_macos and self.platform.is_arm64 and self.requested_version < (
        87, 0, 0, 0):
      raise ValueError(
          "Chrome Arm64 Apple Silicon is only available starting with M87, "
          f"but requested {self.requested_version_str}")
    archive_url = self._find_archive_url()
    if not archive_url:
      raise ValueError(
          f"Could not find matching version for {self.requested_version}")
    self._download_and_extract(archive_url)

  def _find_archive_url(self) -> Optional[str]:
    platform = "mac-universal"
    milestone: int = self.requested_version[0]
    # Quick probe for complete versions
    if self.requested_exact_version:
      test_url = f"{self.URL}{self.version_identifier}/{platform}/"
      logging.info("LIST VERSION for M%s (fast): %s", milestone, test_url)
      maybe_archive_url = self._filter_candidates([test_url])
      if maybe_archive_url:
        return maybe_archive_url
    list_url = f"{self.URL}{milestone}.*/{platform}/"
    logging.info("LIST ALL VERSIONS for M%s (slow): %s", milestone, list_url)
    try:
      listing: List[str] = self.platform.sh_stdout(
          "gsutil", "ls", "-d", list_url).strip().splitlines()
    except helper.SubprocessError as e:
      if "One or more URLs matched no objects" in str(e):
        raise ValueError(
            f"Could not find version {self.requested_version_str} "
            f"for {self.platform.short_name} {self.platform.machine} ") from e
      raise
    logging.info("FILTERING %d CANDIDATES", len(listing))
    return self._filter_candidates(listing)

  def _filter_candidates(self, listing: List[str]) -> Optional[str]:
    version_items = []
    for url in listing:
      version_str, _ = url[len(self.URL):].split("/", maxsplit=1)
      version = tuple(map(int, version_str.split(".")))
      version_items.append((version, url))
    version_items.sort(reverse=True)
    # Iterate from new to old version and and the first one that is older or
    # equal than the requested version.
    for version, url in version_items:
      if not self._version_matches(version):
        logging.debug("Skipping download candidate: %s %s", version, url)
        continue
      version_str = ".".join(map(str, version))
      archive_url = f"{url}GoogleChrome-{version_str}.dmg"
      try:
        result = self.platform.sh_stdout("gsutil", "ls", archive_url)
      except helper.SubprocessError:
        continue
      if result:
        return archive_url
    return None

  def _version_matches(self, version: Tuple[int, int, int, int]) -> bool:
    # Iterate over the version parts. Use 9999 as placeholder to accept
    # an arbitrary version part.
    #
    # Requested: 100.0.4000.500
    # version:   100.0.4000.501 => False
    #
    # Requested: 100.0.4000.ANY_MARKER
    # version:   100.0.4000.501 => True
    # version:   100.0.4001.501 => False
    # version:   101.0.4000.501 => False
    #
    # We assume that the user iterates over a sorted list from new to old
    # versions for a matching milestone.
    for got, expected in zip(version, self.requested_version):
      if expected == self.ANY_MARKER:
        continue
      if got != expected:
        return False
    return True

  def _download_and_extract(self, archive_url: str):
    with tempfile.TemporaryDirectory(prefix="crossbench_download") as tmp_path:
      tmp_dir = pathlib.Path(tmp_path)
      archive_path = self._download_archive(archive_url, tmp_dir)
      self._extract_archive(archive_path)

  def _download_archive(
      self,
      archive_url: str,
      tmp_dir: pathlib.Path,
  ) -> pathlib.Path:
    logging.info("DOWNLOADING %s", archive_url)
    self.platform.sh("gsutil", "cp", archive_url, tmp_dir)
    archive_path = list(tmp_dir.glob("*.dmg"))[0]
    return archive_path

  def _extract_archive(self, archive_path: pathlib.Path):
    result = self.platform.sh_stdout("hdiutil", "attach", "-plist",
                                     archive_path).strip()
    data = plistlib.loads(str.encode(result))
    dmg_path: Optional[pathlib.Path] = None
    for item in data["system-entities"]:
      mount_point = item.get("mount-point", None)
      if mount_point:
        dmg_path = pathlib.Path(mount_point)
        if dmg_path.exists():
          break
    if not dmg_path:
      raise ValueError("Could not mount downloaded disk image")
    apps = list(dmg_path.glob("*.app"))
    assert len(apps) == 1, "Mounted disk image contains more than 1 app"
    app = apps[0]
    try:
      logging.info("COPYING BROWSER src=%s dst=%s", app, self.path)
      shutil.copytree(app, self.path, dirs_exist_ok=False)
    finally:
      self.platform.sh("hdiutil", "detach", dmg_path)
      dmg_path.unlink()
