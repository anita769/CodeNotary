"""Baseline public tests for the vendored packaging subset (D5 bug B).

Prefix-matching behaviors stable across the #673 fix.
License: Apache-2.0 OR BSD-2-Clause (pypa/packaging dual license).
"""

import unittest

from d5b_specifiers import SpecifierSet
from d5b_version import Version

CASES = [
    ("1.0.1", "==1.0.*", True), ("1.1", "==1.0.*", False),
    ("2!1.0.1", "==2!1.0.*", True), ("1.0+local", "==1.0.*", True),
    ("1!0.1", "==1!0.*", True), ("2.0", "==1.*", False),
    ("1.5", ">=1.0,<=2.0", True), ("2.5", ">=1.0,<=2.0", False),
    ("1.0", "==1.0", True), ("2.0", "!=2.1", True),
]


class TestSpecifierBaselineB(unittest.TestCase):
    def test_public_truth_table(self):
        for version, spec, expected in CASES:
            with self.subTest(version=version, spec=spec):
                self.assertEqual(
                    Version(version) in SpecifierSet(spec), expected)


if __name__ == "__main__":
    unittest.main()
