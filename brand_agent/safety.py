from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SafetyFinding:
    rule: str
    matched_text: str


RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(r"\b(ignore|disregard|forget)\s+(all\s+)?(previous|prior|system|developer)\s+instructions?\b", re.I),
    ),
    (
        "credential_exfiltration",
        re.compile(r"\b(send|upload|reveal|print|exfiltrate)\b.{0,50}\b(api[_ -]?key|password|credential|secret|token)\b", re.I),
    ),
    (
        "tool_execution_request",
        re.compile(r"\b(run|execute|invoke|call)\b.{0,35}\b(shell|terminal|command|tool|powershell|cmd\.exe)\b", re.I),
    ),
    (
        "external_action_request",
        re.compile(r"\b(post|publish|send|message|email|delete|purchase)\b.{0,45}\b(on my behalf|without approval|immediately)\b", re.I),
    ),
)


def scan_untrusted_text(*values: str) -> list[SafetyFinding]:
    text = "\n".join(value or "" for value in values)
    findings: list[SafetyFinding] = []
    for rule, pattern in RULES:
        match = pattern.search(text)
        if match:
            findings.append(SafetyFinding(rule=rule, matched_text=match.group(0)[:160]))
    return findings

