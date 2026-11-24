"""agentcore must stay generic: no hospital imports, no domain vocabulary, no agent names or model names."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import agentcore

PKG = Path(agentcore.__file__).parent
FILES = sorted(PKG.glob("*.py"))
FORBIDDEN_MODULES = ("mock_hospital", "agent_service", "tests", "evals")
FORBIDDEN_WORDS = re.compile(
    r"\b(doctors?|appointments?|bills?|billing|hospital|patients?|emergency|handoff|hand-off|prescriptions?|"
    r"city\s?care|booking|haiku|sonnet|opus)\b",
    re.IGNORECASE,
)


def test_package_has_files():
    assert len(FILES) >= 10


def test_no_forbidden_imports():
    for path in FILES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in FORBIDDEN_MODULES, f"{path.name} imports {name}"


def test_no_domain_vocabulary_or_model_names():
    hits = []
    for path in FILES:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN_WORDS.search(line):
                hits.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not hits, "domain words leaked into agentcore:\n" + "\n".join(hits)


def test_public_api_exports_resolve():
    missing = [n for n in agentcore.__all__ if not hasattr(agentcore, n)]
    assert not missing
