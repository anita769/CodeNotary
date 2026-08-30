"""Baseline public tests for the vendored packaging subset (D5 bug A).

Adapted from pypa/packaging tests/test_specifiers.py @ db291c7^ (public
behavior that predates the local-segment LEQ/GEQ fix and is unchanged by
it). License: Apache-2.0 OR BSD-2-Clause (pypa/packaging dual license).
"""

import unittest

from d5a_specifiers import SpecifierSet
from d5a_version import Version

CASES = [
    ("1.0", "==1.0", True), ("1.1", "==1.0", False),
    ("2.0", "!=2.1", True), ("2.1", "!=2.1", False),
    ("1.0", "<2", True), ("2.0", "<2.1", True), ("2.1", "<2.1", False),
    ("3.0", ">2.1", True), ("2.0", ">2.1", False),
    ("1.0.1", "==1.0.*", True), ("1.1", "==1.0.*", False),
    ("1.5", ">=1.0,<=2.0", True), ("2.5", ">=1.0,<=2.0", False),
    ("2.0", "<=2.1", True), ("2.2", "<=2.1", False),
    ("2.1", ">=2.1", True), ("2.0", ">=2.1", False),
]


class TestSpecifierSetBaseline(unittest.TestCase):
    def test_public_truth_table(self):
        for version, spec, expected in CASES:
            with self.subTest(version=version, spec=spec):
                self.assertEqual(
                    Version(version) in SpecifierSet(spec), expected)


if __name__ == "__main__":
    unittest.main()
