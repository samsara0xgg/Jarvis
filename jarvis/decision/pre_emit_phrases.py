"""Single source of truth for limitation/completion regexes.

Per ADR-0002 § Canonical limitation phrasing (lines 344-372). ADR-0001
F4 / F5, ADR-0002 K5 / L3 all reference these constants; no inline
regex literals are allowed elsewhere. The canary
``test_canary_regex_constants_single_source`` AST-scans test files for
inline limitation/completion regex literals and fails them — three
divergent regex sets across docs is what created the F4/K5/L3 drift in
the first place.

Layer rules: stdlib only. Imported by ``jarvis.decision.__init__``
(scrub set) and by the scenario / unit / canary tests under ``tests/``.
"""

from __future__ import annotations

import re
from typing import Final

LIMITATION_REGEXES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"reported,?\s*not\s+verified", re.IGNORECASE),
    re.compile(r"agent\s+reported"),
    re.compile(r"未验证"),
    re.compile(r"没验证"),
    re.compile(r"测试.{0,4}没过"),
    re.compile(r"还没验"),
)

COMPLETION_REGEXES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^完成"),
    re.compile(r"已完成(?!\s*报告)"),
    re.compile(r"\bverified\b", re.IGNORECASE),
    re.compile(r"\bdone\b", re.IGNORECASE),
)

__all__ = ["COMPLETION_REGEXES", "LIMITATION_REGEXES"]
