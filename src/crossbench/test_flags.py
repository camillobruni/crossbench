# Copyright 2022 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from math import fabs
import unittest
from crossbench import flags

from crossbench.flags import Flags, ChromeFeatures, ChromeFlags, JSFlags

class TestFlags(unittest.TestCase):

  CLASS = Flags

  def test_construct(self):
    flags = self.CLASS()
    self.assertEqual(len(flags), 0)
    self.assertFalse('foo' in flags)

  def test_construct_dict(self):
    flags = self.CLASS({ '--foo' : 'v1', '--bar': 'v2'})
    self.assertTrue('--foo' in flags)
    self.assertTrue('--bar' in flags)
    self.assertEqual(flags['--foo'], 'v1')
    self.assertEqual(flags['--bar'], 'v2')

  def test_construct_list(self):
    flags = self.CLASS(('--foo', '--bar'))
    self.assertTrue('--foo' in flags)
    self.assertTrue('--bar' in flags)
    self.assertEqual(flags['--foo'], None)
    self.assertEqual(flags['--bar'], None)
    with self.assertRaises(AssertionError):
       self.CLASS(('--foo=v1', '--bar=v2'))
    flags = self.CLASS((('--foo', 'v3'), '--bar'))
    self.assertEqual(flags['--foo'], 'v3')
    self.assertEqual(flags['--bar'], None)

  def test_construct_flags(self):
    original_flags = self.CLASS({ '--foo' : 'v1', '--bar': 'v2'})
    flags = self.CLASS(original_flags)
    self.assertTrue('--foo' in flags)
    self.assertTrue('--bar' in flags)
    self.assertEqual(flags['--foo'], 'v1')
    self.assertEqual(flags['--bar'], 'v2')

  def test_set(self):
    flags = self.CLASS()
    flags['--foo'] = 'v1'
    with self.assertRaises(AssertionError):
      flags['--foo'] = 'v2'
    # setting the same value is ok
    flags['--foo'] = 'v1'
    self.assertEqual(flags['--foo'], 'v1')
    flags.set('--bar')
    self.assertTrue('--foo' in flags)
    self.assertTrue('--bar' in flags)
    self.assertEqual(flags['--bar'], None)
    with self.assertRaises(AssertionError):
      flags.set('--bar', 'v3')
    flags.set('--bar', 'v4', override=True)
    self.assertEqual(flags['--foo'], 'v1')
    self.assertEqual(flags['--bar'], 'v4')

  def test_get_list(self):
    flags = self.CLASS({ '--foo' : 'v1', '--bar': None})
    self.assertEqual(list(flags.get_list()), ['--foo=v1', '--bar'])

  def test_copy(self):
    flags = self.CLASS({ '--foo' : 'v1', '--bar': None})
    copy = flags.copy()
    self.assertEqual(list(flags.get_list()), list(copy.get_list()))

  def test_update(self):
    flags = self.CLASS({ '--foo' : 'v1', '--bar': None})
    with self.assertRaises(AssertionError):
      flags.update({'--bar': 'v2'})
    self.assertEqual(flags['--foo'], 'v1')
    self.assertEqual(flags['--bar'], None)
    flags.update({'--bar': 'v2'}, override=True)
    self.assertEqual(flags['--foo'], 'v1')
    self.assertEqual(flags['--bar'], 'v2')


class TestChromeFlags(TestFlags):

  CLASS = ChromeFlags

  def test_js_flags(self):
    flags = self.CLASS({'--foo':None, '--bar':'v1', })
    self.assertEqual(flags['--foo'], None)
    self.assertEqual(flags['--bar'], 'v1')
    self.assertFalse('--js-flags' in flags)
    with self.assertRaises(AssertionError):
      flags['--js-flags'] = '--js-foo, --no-js-foo'
    flags['--js-flags'] = '--js-foo=v3, --no-js-bar'
    with self.assertRaises(AssertionError):
      flags['--js-flags'] = '--js-foo=v4, --no-js-bar'
    js_flags = flags.js_flags
    self.assertEqual(js_flags['--js-foo'], 'v3')
    self.assertEqual(js_flags['--no-js-bar'], None)

  def test_js_flags_initial_data(self):
    flags = self.CLASS({'--js-flags': '--foo=v1,--no-bar', })
    js_flags = flags.js_flags
    self.assertEqual(js_flags['--foo'], 'v1')
    self.assertEqual(js_flags['--no-bar'], None)


  def test_features(self):
    flags = self.CLASS()
    features = flags.features
    self.assertTrue(features.is_empty)
    flags["--enable-features"] = "F1,F2"
    with self.assertRaises(AssertionError):
      flags["--disable-features"] = "F1,F2"
    with self.assertRaises(AssertionError):
      flags["--disable-features"] = "F2,F1"
    flags["--disable-features"] = "F3,F4"
    self.assertEqual(features.enabled, {'F1':None, 'F2':None})
    self.assertEqual(features.disabled, set(('F3', 'F4')))


class TestJSFlags(TestFlags):

  CLASS = JSFlags

  def test_conflicting_flags(self):
    with self.assertRaises(AssertionError):
      flags = self.CLASS(('--foo', '--no-foo'))
    with self.assertRaises(AssertionError):
      flags = self.CLASS(('--foo', '--nofoo'))
    flags = self.CLASS(('--foo', '--no-bar'))
    self.assertEqual(flags['--foo'], None)
    self.assertEqual(flags['--no-bar'], None)
    self.assertTrue('--foo' in flags)
    self.assertFalse('--no-foo' in flags)
    self.assertFalse('--bar' in flags)
    self.assertTrue('--no-bar' in flags)

  def test_conflicting_override(self):
    flags = self.CLASS(('--foo', '--no-bar'))
    with self.assertRaises(AssertionError):
      flags.set('--no-foo')
    with self.assertRaises(AssertionError):
      flags.set('--nofoo')
    flags.set('--nobar')
    with self.assertRaises(AssertionError):
      flags.set('--bar')
    with self.assertRaises(AssertionError):
      flags.set('--foo', 'v2')
    self.assertEqual(flags['--foo'], None)
    self.assertEqual(flags['--no-bar'], None)
    flags.set('--no-foo', override=True)
    self.assertFalse('--foo' in flags)
    self.assertTrue('--no-foo' in flags)
    self.assertFalse('--bar' in flags)
    self.assertTrue('--no-bar' in flags)