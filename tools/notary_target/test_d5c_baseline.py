"""Baseline public tests for the vendored packaging subset (D5 bug C).

Compatible-release (~=) and general specifier behaviors stable across the
#100 fix. License: Apache-2.0 OR BSD-2-Clause (pypa/packaging).
"""

import unittest

from d5c_specifiers import SpecifierSet
from d5c_version import Version

CASES = [
    ("1.0", "~=1.0a1", True), ("1.0a1", "~=1.0a1", True),
    ("0.9", "~=1.0a1", False), ("1.1", "~=1.0", True),
    ("2.0", "~=1.0", False), ("1.0.5", "~=1.0.1", True),
    ("1.5", "~=1.0.1", False), ("1.0", "==1.0", True),
    ("1.1", "==1.0", False), ("2.0", "!=2.1", True),
]


class TestCompatibleReleaseBaseline(unittest.TestCase):
    def test_public_truth_table(self):
        for version, spec, expected in CASES:
            with self.subTest(version=version, spec=spec):
                self.assertEqual(
                    Version(version) in SpecifierSet(spec), expected)


if __name__ == "__main__":
    unittest.main()
