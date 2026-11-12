"""Policy-based permission checking.

Loads per-tool policies from config/policies.yaml and answers:
"Is this tool allowed to perform this operation on this resource?"
"""

from __future__ import annotations

import fnmatch
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import yaml

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")
POLICIES_FILE = os.path.join(CONFIG_DIR, "policies.yaml")


class PolicyEngine:
    def __init__(self, policies_file: str = POLICIES_FILE) -> None:
        self.policies_file = policies_file
        self.policies: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        with open(self.policies_file, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("tools", {})

    def get_policy(self, tool_name: str) -> Optional[Dict[str, Any]]:
        return self.policies.get(tool_name)

    def _matches(self, resource: str, patterns: list[str]) -> bool:
        if not patterns:
            return False
        for pat in patterns:
            if fnmatch.fnmatch(resource, pat):
                return True
        return False

    def check(
        self,
        tool_name: str,
        operation: str,
        resource: str,
    ) -> tuple[bool, str]:
        """Return (allowed, reason)."""
        policy = self.get_policy(tool_name)
        if not policy:
            return False, f"no policy defined for tool '{tool_name}'"

        deny = policy.get("deny", {}) or {}
        allow = policy.get("allow", {}) or {}

        # For network operations, match against the hostname so that a full
        # URL (e.g. https://api.weatherapi.com/v1) matches the allow pattern
        # "api.weatherapi.com".
        match_resource = resource
        if operation == "network":
            host = urlparse(resource).hostname or resource
            match_resource = host

        # Deny rules take precedence.
        deny_resources = deny.get(operation, [])
        if self._matches(match_resource, deny_resources):
            return False, f"denied by policy ({operation}: {resource})"

        # Undetermined target (empty resource) for an operation the tool is
        # declared to use -> allow; the policy governs the tool's declared
        # behaviour. Deny rules above still apply first.
        allow_resources = allow.get(operation, [])
        if not resource and allow_resources:
            return True, "allowed by policy (declared behaviour)"

        # If there are explicit allow rules for this operation, require a match.
        if allow_resources:
            if self._matches(match_resource, allow_resources):
                return True, "allowed by policy"
            return False, f"not allowed by policy ({operation}: {resource})"

        # No allow rules for this operation -> default deny.
        return False, f"no allow rule for operation '{operation}'"
