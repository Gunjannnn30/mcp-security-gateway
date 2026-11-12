"""Security event logger.

Writes one JSON object per line to logs/security.log so the project can
answer "why was this action allowed or blocked?" during evaluation.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict

LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
LOG_FILE = os.path.join(LOG_DIR, "security.log")


class SecurityLogger:
    def __init__(self, log_file: str = LOG_FILE) -> None:
        self.log_file = log_file
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)

    def log(self, event: Dict[str, Any]) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        with open(self.log_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def security_event(
        self,
        *,
        tool: str,
        operation: str,
        decision: str,
        reason: str,
        policy_result: str = "",
        runtime_event: str = "",
        anomaly_score: float | None = None,
        text_result: str = "",
        extra: Dict[str, Any] | None = None,
    ) -> None:
        self.log(
            {
                "tool": tool,
                "operation": operation,
                "decision": decision,  # allow | warn | block
                "reason": reason,
                "policy_result": policy_result,
                "runtime_event": runtime_event,
                "anomaly_score": anomaly_score,
                "text_result": text_result,
                **(extra or {}),
            }
        )
