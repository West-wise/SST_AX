"""Model policy pins, real CLI argv construction, and numeric usage boundaries."""
import copy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import model_routing as routing
import codex_impact
import sstc_worker
SESSION = "0199a213-81c0-7800-8aa1-bbab2a035a53"

class ModelRoutingTest(unittest.TestCase):
    def log(self):
        return {"model_routing": routing.new_task_routing()}

    def test_required_analysis_and_implementation_use_sol_medium(self):
        for stage in ("analysis", "implementation"):
            route = routing.route_for(self.log(), stage, "MEDIUM")
            self.assertEqual((route["model"], route["effort"]), ("gpt-6.1-sol", "medium"))

    def test_high_risk_never_uses_text_model(self):
        for stage in ("analysis", "implementation", "text"):
            for risk in ("HIGH", "CRITICAL"):
                route = routing.route_for(self.log(), stage, risk)
                self.assertEqual((route["model"], route["effort"]), ("gpt-6.1-sol", "high"))

    def test_text_route_does_not_replace_essential_analysis(self):
        route = routing.route_for(self.log(), "text", "LOW")
        self.assertEqual((route["model"], route["effort"]), ("gpt-6-luna", "low"))
        self.assertNotEqual(route, routing.route_for(self.log(), "analysis", "LOW"))

    def test_legacy_commands_keep_model_unspecified(self):
        self.assertIsNone(routing.route_for({}, "analysis", "MEDIUM"))
        self.assertNotIn("--model", codex_impact.command("codex", Path("schema.json"), SESSION))
        self.assertNotIn("--model", sstc_worker.codex_command("codex", SESSION))

    def test_initial_and_resume_commands_explicitly_select_route(self):
        for stage in ("analysis", "implementation"):
            route = routing.route_for(self.log(), stage, "MEDIUM")
            args = (codex_impact.command("codex", Path("schema.json"), SESSION, route)
                    if stage == "analysis" else sstc_worker.codex_command("codex", SESSION, route))
            self.assertEqual(args[args.index("--model") + 1], "gpt-6.1-sol")
            self.assertIn('model_reasoning_effort="medium"', args)
            self.assertIn("--ignore-user-config", args)
            self.assertIn('approval_policy="never"', args)
            self.assertEqual(args[args.index("resume") + 1], SESSION)
            self.assertIn("read-only" if stage == "analysis" else "workspace-write", args)

    def test_changed_or_unknown_pin_is_refused(self):
        for pin in (None, {}, {"profile": "unknown"},
                    {"profile": "balanced-v1", "policy_sha256": "0" * 64}):
            with self.assertRaises(ValueError):
                routing.route_for({"model_routing": pin}, "analysis", "MEDIUM")

    def test_arbitrary_model_or_effort_is_refused(self):
        original = routing.route_for(self.log(), "implementation", "MEDIUM")
        for key, value in (("model", "arbitrary"), ("effort", "max"), ("stage", "shell")):
            route = copy.deepcopy(original); route[key] = value
            with self.assertRaises(ValueError):
                routing.route_arguments(route)

    def test_new_task_analysis_cannot_omit_route(self):
        with self.assertRaisesRegex(ValueError, "MODEL_ROUTING_ANALYSIS_CHANGED"):
            routing.validate_analysis_route({"risk_level": "MEDIUM"}, self.log(), {})

    def test_usage_keeps_only_numeric_counters(self):
        raw = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20,
            "cached_input_tokens": 10, "output_tokens": 5, "reasoning_tokens": True,
            "unexpected_field": "ignored", "model": "unverified"}}).encode()
        self.assertEqual(routing.usage_summary(raw), {"input_tokens": 20, "cached_input_tokens": 10, "output_tokens": 5})

    def test_usage_absence_is_not_reported_as_zero(self):
        self.assertIsNone(routing.usage_summary(b'{"type":"turn.completed"}'))
        self.assertIsNone(routing.usage_summary(b'{"type":"turn.completed","usage":{"input_tokens":-1,"output_tokens":"5"}}'))

    def test_analysis_route_survives_legitimate_policy_risk_update(self):
        log = self.log()
        route = routing.route_for(log, "analysis", "MEDIUM")
        routing.validate_analysis_route({"risk_level": "HIGH"}, log,
            {"model_route": route, "routing_risk": "MEDIUM"})

    def test_validated_high_risk_result_selects_high_implementation(self):
        context = {"state": {"risk_level": "LOW"}, "result": {"risk_level": "HIGH"}, "log": self.log()}
        self.assertEqual(routing.implementation_route(context)["effort"], "high")
