"""Trusted, versioned model selection. No network calls or authority decisions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

DEFAULT_PROFILE = "balanced-v1"
POLICY = Path(__file__).resolve().parents[1] / "policies/model-routing/balanced-v1.json"


def policy() -> tuple[dict, str]:
    raw = POLICY.read_bytes()
    value = json.loads(raw)
    if value != {
        "profile": DEFAULT_PROFILE,
        "analysis": {"model": "gpt-6.1-sol", "effort": "medium"},
        "implementation": {"model": "gpt-6.1-sol", "effort": "medium"},
        "high_risk": {"model": "gpt-6.1-sol", "effort": "high"},
        "text": {"model": "gpt-6-luna", "effort": "low"},
    }:
        raise ValueError("MODEL_ROUTING_POLICY_INVALID")
    return value, hashlib.sha256(raw).hexdigest()


def new_task_routing() -> dict:
    _, sha = policy()
    return {"profile": DEFAULT_PROFILE, "policy_sha256": sha}


def route_for(log: dict, stage: str, risk: str) -> dict | None:
    if stage not in {"analysis", "implementation", "text"} or risk not in {
        "NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"
    }:
        raise ValueError("MODEL_ROUTING_CONTEXT_INVALID")
    if "model_routing" not in log:
        return None  # Existing Tasks retain their original CLI/session behavior.
    if log["model_routing"] != new_task_routing():
        raise ValueError("MODEL_ROUTING_PIN_CHANGED")
    value, _ = policy()
    tier = "high_risk" if risk in {"HIGH", "CRITICAL"} else stage
    return {**log["model_routing"], "stage": stage, "tier": tier, **value[tier]}


def route_arguments(route: dict | None) -> list[str]:
    if route is None:
        return []
    if not isinstance(route, dict):
        raise ValueError("MODEL_ROUTING_ROUTE_INVALID")
    stage, tier = route.get("stage"), route.get("tier")
    risk = "HIGH" if tier == "high_risk" else "MEDIUM"
    expected = route_for({"model_routing": new_task_routing()}, stage, risk)
    if route != expected:
        raise ValueError("MODEL_ROUTING_ROUTE_INVALID")
    return ["--model", route["model"], "-c", 'model_reasoning_effort="' + route["effort"] + '"']


def validate_analysis_route(state: dict, log: dict, record: dict) -> None:
    if record.get("model_route") != route_for(log, "analysis", record.get("routing_risk", state["risk_level"])):
        raise ValueError("MODEL_ROUTING_ANALYSIS_CHANGED")


def usage_summary(raw: bytes) -> dict | None:
    """Keep numeric counters only; never persist raw model/tool event streams."""
    found = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        counters = {key: usage[key] for key in (
            "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"
        ) if type(usage.get(key)) is int and 0 <= usage[key] <= 2**63 - 1}
        if counters:
            found = counters
    return found


def implementation_route(context: dict) -> dict | None:
    risk = "HIGH" if any(r in {"HIGH", "CRITICAL"} for r in
                         (context["state"]["risk_level"], context["result"]["risk_level"])) else "MEDIUM"
    return route_for(context["log"], "implementation", risk)
