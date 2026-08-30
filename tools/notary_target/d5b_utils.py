"""Vendored subset: canonicalize_version verbatim from pypa/packaging utils.py @ a6c9bc4^.

License: Apache-2.0 OR BSD-2-Clause.
"""

import re
from typing import Union

from d5b_version import InvalidVersion, Version

NormalizedVersion = Union[Version, str]


def canonicalize_version(
    version: Union[Version, str], *, strip_trailing_zero: bool = True
) -> str:
    """
    This is very similar to Version.__str__, but has one subtle difference
    with the way it handles the release segment.
    """
    if isinstance(version, str):
        try:
            parsed = Version(version)
        except InvalidVersion:
            # Legacy versions cannot be normalized
            return version
    else:
        parsed = version

    parts = []

    # Epoch
    if parsed.epoch != 0:
        parts.append(f"{parsed.epoch}!")

    # Release segment
    release_segment = ".".join(str(x) for x in parsed.release)
    if strip_trailing_zero:
        # NB: This strips trailing '.0's to normalize
        release_segment = re.sub(r"(\.0)+$", "", release_segment)
    parts.append(release_segment)

    # Pre-release
    if parsed.pre is not None:
        parts.append("".join(str(x) for x in parsed.pre))

    # Post-release
    if parsed.post is not None:
        parts.append(f".post{parsed.post}")

    # Development release
    if parsed.dev is not None:
        parts.append(f".dev{parsed.dev}")

    # Local version segment
    if parsed.local is not None:
        parts.append(f"+{parsed.local}")

    return "".join(parts)
