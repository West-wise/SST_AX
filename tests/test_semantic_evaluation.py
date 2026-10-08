"""Schema-valid false negatives must fail human-labelled semantic evaluation."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import evaluate_impact as evaluation
from impact_validation import load_document, validate_impact

ROOT = Path(__file__).resolve().parents[1]


class SemanticEvaluationTest(unittest.TestCase):
    def test_schema_valid_no_impact_is_not_proof_of_unit_compatibility(self):
        fixture = load_document(ROOT / "tests/fixtures/impact/sstd-not-required.json")
        self.assertEqual(validate_impact(fixture["task"], fixture["manifest"], fixture["result"]), [])
        case = load_document(ROOT / "tests/fixtures/semantic/uptime-unit.json")
        failures = evaluation.evaluate(case, fixture["result"])
        self.assertIn("CHANGE_REQUIRED", failures)
        self.assertIn("IMPACT_UI_UX", failures)
        self.assertIn("IMPACT_PROTOCOL_CONTRACT", failures)

    def test_daemon_only_change_accepts_no_client_work(self):
        fixture = load_document(ROOT / "tests/fixtures/impact/sstd-not-required.json")
        case = load_document(ROOT / "tests/fixtures/semantic/daemon-log-only.json")
        self.assertEqual(evaluation.evaluate(case, fixture["result"]), [])

    def test_labelled_contract_change_requires_approval_reasons(self):
        fixture = load_document(ROOT / "tests/fixtures/impact/sstd-required.json")
        case = load_document(ROOT / "tests/fixtures/semantic/field-width.json")
        result = copy.deepcopy(fixture["result"])
        result["change_required"] = "REQUIRED"
        result["impacts"]["protocol_contract"]["status"] = "PRESENT"
        result["approval_reasons"] = ["PROTOCOL_CHANGE"]
        self.assertEqual(evaluation.evaluate(case, result), [])
        result["approval_reasons"] = []
        self.assertIn("APPROVAL_REASONS", evaluation.evaluate(case, result))

    def test_missing_decoder_never_scores_as_no_impact(self):
        fixture = load_document(ROOT / "tests/fixtures/impact/sstd-not-required.json")
        case = load_document(ROOT / "tests/fixtures/semantic/missing-decoder.json")
        self.assertIn("CHANGE_REQUIRED", evaluation.evaluate(case, fixture["result"]))

    def test_complete_unit_case_rejects_omitted_ui_and_extra_server_work(self):
        case = load_document(ROOT / "tests/fixtures/semantic/uptime-unit-complete.json")
        result = load_document(ROOT / "tests/fixtures/impact/sstd-required.json")["result"]
        result["impacts"]["ui_ux"]["status"] = "PRESENT"
        result["approval_reasons"].append("UI_CHANGE")
        self.assertEqual(evaluation.evaluate(case, result), [])
        missing_ui = copy.deepcopy(result)
        missing_ui["impacts"]["ui_ux"]["status"] = "ABSENT"
        self.assertIn("IMPACT_UI_UX", evaluation.evaluate(case, missing_ui))
        extra_server = copy.deepcopy(result)
        extra_server["impacts"]["sstd_change_required"]["status"] = "PRESENT"
        self.assertIn("IMPACT_SSTD_CHANGE_REQUIRED", evaluation.evaluate(case, extra_server))

    def test_complete_width_case_rejects_extra_ui_and_server_work(self):
        case = load_document(ROOT / "tests/fixtures/semantic/field-width-complete.json")
        result = load_document(ROOT / "tests/fixtures/impact/sstd-required.json")["result"]
        self.assertEqual(evaluation.evaluate(case, result), [])
        for name in ("ui_ux", "sstd_change_required"):
            with self.subTest(impact=name):
                changed = copy.deepcopy(result)
                changed["impacts"][name]["status"] = "PRESENT"
                self.assertIn("IMPACT_" + name.upper(), evaluation.evaluate(case, changed))


if __name__ == "__main__":
    unittest.main()
