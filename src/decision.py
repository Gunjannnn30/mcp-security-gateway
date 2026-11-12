"""Combined risk-based security decision.

Merges signals from policy, runtime monitoring, anomaly detection, and text
detection into a single allow / warn / block decision, mirroring the synopsis
flow: what the tool is allowed to do -> what it claims -> what it actually
does -> how unusual its behaviour is -> whether messages are manipulative.
"""

from __future__ import annotations

from typing import Any

# Decision outcomes
ALLOW = "allow"
WARN = "warn"
BLOCK = "block"


class DecisionEngine:
    def __init__(
        self,
        *,
        anomaly_threshold: float = 0.3,
        text_malicious_action: str = "block",
    ) -> None:
        self.anomaly_threshold = anomaly_threshold
        self.text_malicious_action = text_malicious_action

    def decide(
        self,
        *,
        policy_allowed: bool,
        policy_reason: str,
        runtime_suspicious: bool,
        runtime_detail: str,
        anomaly_anomalous: bool,
        anomaly_score: float,
        text_suspicious: bool,
        text_reason: str,
    ) -> tuple[str, str, list[str]]:
        """Return (decision, reason, signals).

        decision: allow | warn | block
        """
        signals: list[str] = []

        # 1. Policy (deterministic) - hard deny.
        if not policy_allowed:
            return BLOCK, f"policy: {policy_reason}", ["policy"]

        # 2. Text detector - malicious instructions in description/response.
        if text_suspicious:
            action = self.text_malicious_action
            signals.append("text")
            if action == BLOCK:
                return BLOCK, f"text: {text_reason}", signals
            if action == WARN:
                signals.append("text-warning")
                return WARN, f"text warning: {text_reason}", signals

        # 3. Runtime behaviour mismatch.
        if runtime_suspicious:
            signals.append("runtime")
            return BLOCK, f"runtime behaviour: {runtime_detail}", signals

        # 4. ML anomaly - unusual but not automatically malicious.
        if anomaly_anomalous:
            signals.append("anomaly")
            return WARN, f"anomalous behaviour (score {anomaly_score:.2f})", signals

        return ALLOW, "all checks passed", signals
