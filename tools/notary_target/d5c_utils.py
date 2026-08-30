"""Vendored subset: canonicalize_version verbatim from pypa/packaging utils.py @ 7b2bb91^.

License: Apache-2.0 OR BSD-2-Clause.
"""

import re
from typing import Union

from d5c_version import InvalidVersion, Version

NormalizedVersion = Union[Version, str]


def canonicalize_version(version):
    # type: (Union[Version, str]) -> Union[Version, str]
    """
    This is very similar to Version.__str__, but has one subtle difference
    with the way it handles the release segment.
    """
    if not isinstance(version, Version):
        try:
            version = Version(version)
        except InvalidVersion:
            # Legacy versions cannot be normalized
            return version

    parts = []

    # Epoch
    if version.epoch != 0:
        parts.append(f"{version.epoch}!")

    # Release segment
    # NB: This strips trailing '.0's to normalize
    parts.append(re.sub(r"(\.0)+$", "", ".".join(str(x) for x in version.release)))

    # Pre-release
    if version.pre is not None:
        parts.append("".join(str(x) for x in version.pre))

    # Post-release
    if version.post is not None:
        parts.append(f".post{version.post}")

    # Development release
    if version.dev is not None:
        parts.append(f".dev{version.dev}")

    # Local version segment
    if version.local is not None:
        parts.append(f"+{version.local}")

    return "".join(parts)
