"""
test_integrity.py: repository-wide guarantees that are not about any one feature.

1. models.py is the team's shared standard. There must be exactly one copy,
   and it must stay byte-identical to the canonical text in Appendix A of
   HANDSHAKE_BUILD.md. The SHA-256 below was computed from Appendix A at the
   start of the integration and confirmed against the backend's copy.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# SHA-256 of Appendix A's models.py text (and of backend/app/models.py before the move).
CANONICAL_MODELS_SHA256 = "63778dcdb0c369e02f8113573d383156dde892967895578c1338776f007dac02"


def test_models_py_is_byte_identical_to_appendix_a() -> None:
    """handshake/models.py must never be edited, reformatted, or 'fixed'."""
    digest = hashlib.sha256((REPO_ROOT / "handshake" / "models.py").read_bytes()).hexdigest()
    assert digest == CANONICAL_MODELS_SHA256


def test_there_is_exactly_one_models_py() -> None:
    """No second copy of the shared models may exist anywhere in the source tree."""
    skip = {".venv", "venv", "node_modules", ".next", "_incoming", ".git"}
    copies = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in REPO_ROOT.rglob("models.py")
        if not (set(path.relative_to(REPO_ROOT).parts) & skip)
    ]
    # ajay_mcp/models.py is Ajay's as-delivered import; it is removed when his
    # files are integrated (milestone 5), after which only the canonical copy remains.
    copies = [c for c in copies if not c.startswith("ajay_mcp/")]
    assert copies == ["handshake/models.py"]
