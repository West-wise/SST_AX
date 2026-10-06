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


if __name__ == "__main__":
    unittest.main()
