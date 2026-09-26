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
    assert copies == ["handshake/models.py"]


# SHA-256 of Ajay's COMPILER_PROMPT string exactly as delivered in prompts.py.
CANONICAL_COMPILER_PROMPT_SHA256 = "1dda5a8ab6f2a765ab89e0c1fcacc51b9130c5c0be138c3c339db9ac59216f98"


def test_compiler_prompt_is_word_for_word() -> None:
    """COMPILER_PROMPT must stay exactly Ajay's text; new instructions go in separate constants."""
    from handshake.prompts import COMPILER_PROMPT

    assert hashlib.sha256(COMPILER_PROMPT.encode("utf-8")).hexdigest() == CANONICAL_COMPILER_PROMPT_SHA256
