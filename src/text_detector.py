"""Text-based detector for hidden/manipulative instructions.

Scans tool descriptions and tool responses for prompt-injection /
tool-poisoning patterns such as "ignore previous instructions" or
"reveal sensitive information". Returns a result string and a flag.
"""

from __future__ import annotations

import re
from typing import Tuple

# Patterns that indicate a tool is trying to manipulate the AI agent.
MALICIOUS_PATTERNS = [
    r"ignore\s+(the\s+)?(previous|prior|above|system|user)\s+instructions?",
    r"disregard\s+(the\s+)?(previous|prior|above)\s+instructions?",
    r"you\s+are\s+now\s+(a|an)\s+(different|new|other)",
    r"reveal\s+(sensitive|confidential|secret|private)\s+information",
    r"print\s+(the\s+)?(system|api|secret|password|key)\s+(prompt|key|token)",
    r"send\s+(the\s+)?(file|data|contents)\s+to",
    r"download\s+and\s+execute",
    r"exec\s*\(",
    r"eval\s*\(",
    r"__import__\s*\(",
    r"subprocess\.",
    r"os\.system\s*\(",
]


class TextDetector:
    def __init__(self) -> None:
        self._compiled = [re.compile(p, re.IGNORECASE) for p in MALICIOUS_PATTERNS]

    def scan(self, text: str) -> Tuple[bool, str]:
        """Return (is_suspicious, reason)."""
        if not text:
            return False, "clean"
        for pattern in self._compiled:
            if pattern.search(text):
                return True, f"matched malicious pattern: {pattern.pattern}"
        return False, "clean"

    def scan_tool_description(self, description: str) -> Tuple[bool, str]:
        return self.scan(description)

    def scan_tool_response(self, response: str) -> Tuple[bool, str]:
        return self.scan(response)
