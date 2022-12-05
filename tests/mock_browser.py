# Copyright 2022 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from __future__ import annotations

import abc
import copy
import pathlib
from typing import TYPE_CHECKING, List, Optional, Tuple, Type

from crossbench import helper
from crossbench.browsers import (Browser, Chrome, Chromium, Edge, Firefox,
                                 Safari)
from crossbench.flags import ChromeFlags, Flags

if TYPE_CHECKING:
  from crossbench.runner import Run, Runner


class MockBrowser(Browser, metaclass=abc.ABCMeta):
  APP_PATH: pathlib.Path = pathlib.Path("/")
  MACOS_BIN_NAME: str = ""
  VERSION = "100.22.33.44"

  @classmethod
  def setup_fs(cls, fs):
    macos_bin_name = cls.APP_PATH.stem
    if cls.MACOS_BIN_NAME:
      macos_bin_name = cls.MACOS_BIN_NAME
    cls.setup_bin(fs, cls.APP_PATH, macos_bin_name)

  @classmethod
  def setup_bin(cls, fs, bin_path: pathlib.Path, macos_bin_name: str):
    if helper.platform.is_macos:
      assert bin_path.suffix == ".app"
      bin_path = bin_path / "Contents" / "MacOS" / macos_bin_name
    elif helper.platform.is_win:
      assert bin_path.suffix == ".exe"
    fs.create_file(bin_path)

  @classmethod
  def default_flags(cls, initial_data: Flags.InitialDataType = None):
    return ChromeFlags(initial_data)

  def __init__(self,
               label: str,
               path: Optional[pathlib.Path],
               *args,
               browser_name: str = "",
               **kwargs):
    assert browser_name, "Mock browser needs a name / type"
    assert self.APP_PATH
    path = path or pathlib.Path(self.APP_PATH)
    self.app_path = path
    kwargs["type"] = browser_name
    super().__init__(label, path, *args, **kwargs)
    self.url_list: List[str] = []
    self.js_list: List[str] = []
    self.js_side_effect: List[str] = []
    self.run_js_side_effect: List[str] = []
    self.did_run: bool = False
    self.clear_cache_dir: bool = False
    chrome_flags = self.flags
    assert isinstance(chrome_flags, ChromeFlags)
    self.js_flags = chrome_flags.js_flags  # pylint: disable=no-member

  def clear_cache(self, runner: Runner):
    pass

  def start(self, run: Run):
    assert not self._is_running
    self._is_running = True
    self.did_run = True
    self.run_js_side_effect = list(self.js_side_effect)

  def force_quit(self):
    # Assert that start() was called before force_quit()
    assert self._is_running
    self._is_running = False

  def _extract_version(self):
    return self.VERSION

  def user_agent(self, runner: cb.runner.Runner) -> str:
    return f"Mock Browser {self.type}, {self.VERSION}"

  def show_url(self, runner: Runner, url):
    self.url_list.append(url)

  def js(self, runner: Runner, script, timeout=None, arguments=()):
    self.js_list.append(script)
    if self.js_side_effect is None:
      return None
    assert self.run_js_side_effect, (
        "Not enough mock js_side_effect available. "
        "Please add another js_side_effect entry for "
        f"arguments={arguments} \n"
        f"Script: {script}")
    result = self.run_js_side_effect.pop(0)
    # Return copies to avoid leaking data between repetitions.
    return copy.deepcopy(result)


if helper.platform.is_macos:
  APP_ROOT = pathlib.Path("/Applications")
elif helper.platform.is_win:
  APP_ROOT = pathlib.Path("C:/Program Files")
else:
  APP_ROOT = pathlib.Path("/usr/bin")


class MockChromiumBrowser(MockBrowser, metaclass=abc.ABCMeta):
  pass


# Inject MockBrowser into the browser hierarchy for easier testing.
Chromium.register(MockChromiumBrowser)


class MockChromeBrowser(MockChromiumBrowser, metaclass=abc.ABCMeta):

  def __init__(self,
               label: str,
               *args,
               path: Optional[pathlib.Path] = None,
               **kwargs):
    super().__init__(label, path, browser_name="chrome", *args, **kwargs)


Chrome.register(MockChromeBrowser)
assert issubclass(MockChromeBrowser, Chrome)


class MockChromeStable(MockChromeBrowser):
  if helper.platform.is_macos:
    APP_PATH = APP_ROOT / "Google Chrome.app"
  elif helper.platform.is_win:
    APP_PATH = APP_ROOT / "Google/Chrome/Application/chrome.exe"
  else:
    APP_PATH = APP_ROOT / "google-chrome"


assert issubclass(MockChromeStable, Chromium)
assert issubclass(MockChromeStable, Chrome)


class MockChromeBeta(MockChromeBrowser):
  VERSION = "101.22.33.44"
  if helper.platform.is_macos:
    APP_PATH = APP_ROOT / "Google Chrome Beta.app"
  elif helper.platform.is_win:
    APP_PATH = APP_ROOT / "Google/Chrome Beta/Application/chrome.exe"
  else:
    APP_PATH = APP_ROOT / "google-chrome-beta"


class MockChromeDev(MockChromeBrowser):
  VERSION = "102.22.33.44"
  if helper.platform.is_macos:
    APP_PATH = APP_ROOT / "Google Chrome Dev.app"
  elif helper.platform.is_win:
    APP_PATH = APP_ROOT / "Google/Chrome Dev/Application/chrome.exe"
  else:
    APP_PATH = APP_ROOT / "google-chrome-unstable"


class MockChromeCanary(MockChromeBrowser):
  VERSION = "103.22.33.44"
  if helper.platform.is_macos:
    APP_PATH = APP_ROOT / "Google Chrome Canary.app"
  elif helper.platform.is_win:
    APP_PATH = APP_ROOT / "Google/Chrome SxS/Application/chrome.exe"
  else:
    APP_PATH = APP_ROOT / "google-chrome-canary"


class MockEdgeBrowser(MockChromiumBrowser, metaclass=abc.ABCMeta):

  def __init__(self,
               label: str,
               *args,
               path: Optional[pathlib.Path] = None,
               **kwargs):
    super().__init__(label, path, browser_name="edge", *args, **kwargs)


Edge.register(MockEdgeBrowser)
assert issubclass(MockEdgeBrowser, Chromium)
assert issubclass(MockEdgeBrowser, Edge)


class MockEdgeStable(MockEdgeBrowser):
  if helper.platform.is_macos:
    APP_PATH = APP_ROOT / "Microsoft Edge.app"
  elif helper.platform.is_win:
    APP_PATH = APP_ROOT / "Microsoft/Edge/Application/msedge.exe"
  else:
    APP_PATH = APP_ROOT / "microsoft-edge"


class MockEdgeBeta(MockEdgeBrowser):
  VERSION = "101.22.33.44"
  if helper.platform.is_macos:
    APP_PATH = APP_ROOT / "Microsoft Edge Beta.app"
  elif helper.platform.is_win:
    APP_PATH = APP_ROOT / "Microsoft/Edge Beta/Application/msedge.exe"
  else:
    APP_PATH = APP_ROOT / "microsoft-edge-beta"


class MockEdgeDev(MockEdgeBrowser):
  VERSION = "102.22.33.44"
  if helper.platform.is_macos:
    APP_PATH = APP_ROOT / "Microsoft Edge Dev.app"
  elif helper.platform.is_win:
    APP_PATH = APP_ROOT / "Microsoft/Edge Dev/Application/msedge.exe"
  else:
    APP_PATH = APP_ROOT / "microsoft-edge-dev"


class MockEdgeCanary(MockEdgeBrowser):
  VERSION = "103.22.33.44"
  if helper.platform.is_macos:
    APP_PATH = APP_ROOT / "Microsoft Edge Canary.app"
  elif helper.platform.is_win:
    APP_PATH = APP_ROOT / "Microsoft/Edge SxS/Application/msedge.exe"
  else:
    APP_PATH = APP_ROOT / "unssuported/msedge-canary"


class MockSafariBrowser(MockBrowser, metaclass=abc.ABCMeta):

  def __init__(self,
               label: str,
               *args,
               path: Optional[pathlib.Path] = None,
               **kwargs):
    super().__init__(label, path, browser_name="safari", *args, **kwargs)


Safari.register(MockSafariBrowser)
assert issubclass(MockSafariBrowser, Safari)


class MockSafari(MockSafariBrowser):
  if helper.platform.is_macos:
    APP_PATH = APP_ROOT / "Safari.app"
  elif helper.platform.is_win:
    APP_PATH = APP_ROOT / "Unsupported/Safari.exe"
  else:
    APP_PATH = pathlib.Path("/unsupported-platform/Safari")


class MockSafariTechnologyPreview(MockSafariBrowser):
  if helper.platform.is_macos:
    APP_PATH = APP_ROOT / "Safari Technology Preview.app"
  elif helper.platform.is_win:
    APP_PATH = APP_ROOT / "Unsupported/Safari Technology Preview.exe"
  else:
    APP_PATH = pathlib.Path("/unsupported-platform/Safari Technology Preview")


class MockFirefoxBrowser(MockBrowser, metaclass=abc.ABCMeta):

  def __init__(self,
               label: str,
               *args,
               path: Optional[pathlib.Path] = None,
               **kwargs):
    super().__init__(label, path, browser_name="firefox", *args, **kwargs)


Firefox.register(MockFirefoxBrowser)
assert issubclass(MockFirefoxBrowser, Firefox)


class MockFirefox(MockFirefoxBrowser):
  if helper.platform.is_macos:
    APP_PATH = APP_ROOT / "Firefox.app"
  elif helper.platform.is_win:
    APP_PATH = APP_ROOT / "Mozilla Firefox/firefox.exe"
  else:
    APP_PATH = APP_ROOT / "firefox"


class MockFirefoxDeveloperEdition(MockFirefoxBrowser):
  if helper.platform.is_macos:
    APP_PATH = APP_ROOT / "Firefox Developer Edition.app"
  elif helper.platform.is_win:
    APP_PATH = APP_ROOT / "Firefox Developer Edition/firefox.exe"
  else:
    APP_PATH = APP_ROOT / "firefox-developer-edition"


class MockFirefoxNightly(MockFirefoxBrowser):
  if helper.platform.is_macos:
    APP_PATH = APP_ROOT / "Firefox Nightly.app"
  elif helper.platform.is_win:
    APP_PATH = APP_ROOT / "Firefox Nightly/firefox.exe"
  else:
    APP_PATH = APP_ROOT / "firefox-trunk"


ALL: Tuple[Type[MockBrowser], ...] = (
    MockChromeCanary,
    MockChromeDev,
    MockChromeBeta,
    MockChromeStable,
    MockEdgeCanary,
    MockEdgeDev,
    MockEdgeBeta,
    MockEdgeStable,
    MockSafari,
    MockSafariTechnologyPreview,
    MockFirefox,
    MockFirefoxDeveloperEdition,
    MockFirefoxNightly,
)
