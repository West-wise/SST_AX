"""Integration coverage for pinned budgets, publication and verified context."""
import copy
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import execution_policy as policy
import execution_limits as limits
import task_storage as storage
import test_sstc_worker as fixture

class ExecutionProfilesTest(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SstcWorkerTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.path=self.f.task
    def budget(self,label="GITHUB"):
        return policy.Budget(self.path,label)
    def test_new_task_pins_balanced_profile(self):
        self.assertEqual(policy.pair(self.path)[1]["execution_profile"],"balanced-v2")
        proof=policy.execution_context(self.path,self.f.inputs)["proof"]
        self.assertEqual(proof["execution_profile"],"balanced-v2")
        self.assertEqual(proof["limits_sha256"],limits.limits_hash(limits.PROFILES["balanced-v2"]))
    def test_unknown_profile_refused(self):
        state,log=policy.pair(self.path);log["execution_profile"]="unbounded"
        storage.save_pair(self.path,state,log)
        with self.assertRaisesRegex(ValueError,"EXECUTION_PROFILE_REFUSED"):self.budget()
    def test_profile_change_invalidates_authority(self):
        policy.execution_context(self.path,self.f.inputs)
        state,log=policy.pair(self.path);log["execution_profile"]="legacy-v1"
        storage.save_pair(self.path,state,log)
        with self.assertRaisesRegex(ValueError,"EXECUTION_AUTHORITY_CHANGED"):
            policy.execution_context(self.path,self.f.inputs)
    def test_legacy_task_preserves_original_policy_and_counters(self):
        class LegacyFixture(fixture.SstcWorkerTest):
            def run_script(inner,name,*args):
                result=super().run_script(name,*args)
                if name=="create_task.py":
                    state,log=policy.pair(inner.task);log.pop("execution_profile")
                    storage.save_pair(inner.task,state,log)
                return result
        legacy=LegacyFixture();legacy.setUp();self.addCleanup(legacy.doCleanups)
        self.path=legacy.task
        proof=policy.execution_context(self.path,legacy.inputs)["proof"]
        self.assertEqual(proof["policy_sha256"],limits.digest((limits.ROOT/"policies/versions/legacy-v1.md").read_text()))
        self.assertNotIn("execution_profile",proof)
        self.assertNotIn("limits_sha256",proof)
        budget=self.budget();budget.tick(writes=18);budget.finish("INTERRUPTED")
        resumed=self.budget("IMPLEMENTING")
        self.assertEqual(resumed.remaining_writes,2)
        self.assertEqual(resumed.value["write_steps"],18)
        resumed.finish("DONE")
    def test_read_inspections_do_not_consume_mutations(self):
        budget=self.budget();budget.tick(reads=budget.limits.read_steps)
        self.assertEqual(budget.value["write_steps"],0)
        with self.assertRaisesRegex(ValueError,"EXECUTION_BUDGET_EXHAUSTED"):
            budget.require_capacity(reads=1)
        budget.finish("EXHAUSTED")
    def test_read_and_write_counters_survive_resume(self):
        budget=self.budget();budget.tick(writes=2,reads=3);budget.finish("INTERRUPTED")
        resumed=self.budget()
        self.assertEqual((resumed.value["write_steps"],resumed.value["read_steps"]),(2,3))
        resumed.finish("DONE")
    def test_worker_cannot_spend_reserved_publication_budget(self):
        budget=self.budget("IMPLEMENTING");budget.tick(writes=15)
        with self.assertRaisesRegex(ValueError,"EXECUTION_BUDGET_EXHAUSTED"):
            budget.require_capacity(writes=1)
        budget.finish("RESERVED")
        publication=self.budget()
        self.assertEqual(publication.remaining_writes,5)
        publication.finish("DONE")
    def test_remote_reads_and_mutations_use_separate_counters(self):
        class Client:
            def api(self,endpoint,payload=None,**kwargs):return {"ok":True}
        client=policy.BudgetClient(Client(),self.path)
        client.api("read");client.api("write",{})
        record=storage.load_json(storage.companion(self.path,"execution"))
        self.assertEqual((record["read_steps"],record["write_steps"]),(1,1))
    def test_exhausted_remote_mutation_never_reaches_github(self):
        budget=self.budget();budget.tick(writes=20);budget.finish("DONE")
        with patch.object(policy.GitHub,"api") as call:
            with self.assertRaisesRegex(ValueError,"EXECUTION_BUDGET_EXHAUSTED"):
                policy.BudgetClient(policy.GitHub(),self.path).api("write",{})
        call.assert_not_called()
        self.assertEqual(storage.load_json(storage.companion(self.path,"execution"))["write_steps"],20)
        self.assertEqual(policy.pair(self.path)[0]["status"],"IMPLEMENTATION_FAILED")
    def test_invalid_read_count_refused(self):
        for invalid in (True,-1,1.5):
            with self.subTest(value=invalid):
                output=storage.companion(self.path,"execution")
                storage.atomic_json(output,{"active_seconds":0,"attempts":0,"write_steps":0,"read_steps":invalid})
                with self.assertRaisesRegex(ValueError,"EXECUTION_CHECKPOINT_INVALID"):self.budget()
    def test_invalid_tick_never_changes_counters(self):
        budget=self.budget();before=copy.deepcopy(budget.value)
        for kwargs in ({"writes":True},{"reads":-1},{"writes":1.5}):
            with self.assertRaisesRegex(ValueError,"EXECUTION_CHECKPOINT_INVALID"):budget.tick(**kwargs)
        self.assertEqual(budget.value,before);budget.finish("DONE")
    def test_context_preload_uses_verified_bodies_and_separate_read_budget(self):
        ctx=policy.execution_context(self.path,self.f.inputs)
        budget=self.budget("IMPLEMENTING")
        prepared=limits.worker_context(ctx,budget)
        self.assertTrue(prepared["evidence"])
        self.assertEqual((budget.value["read_steps"],budget.value["write_steps"]),(1,0))
        self.assertTrue(all(row["text"]==ctx["bodies"][row["evidence_id"]] for row in prepared["evidence"]))
        budget.finish("DONE")
    def test_context_bounds_preserve_omitted_ids(self):
        ctx=policy.execution_context(self.path,self.f.inputs);budget=self.budget("IMPLEMENTING")
        prepared=limits.worker_context(ctx,budget,max_files=1,max_bytes=65536)
        self.assertLessEqual(len(prepared["evidence"]),1)
        self.assertTrue(prepared["omitted_evidence_ids"])
        budget.finish("DONE")
    def test_combined_retry_limit_does_not_allow_an_extra_worker_retry(self):
        storage.atomic_json(storage.companion(self.path,"execution"),
          {"active_seconds":0,"attempts":1,"analysis_retries":2,"write_steps":0})
        with self.assertRaisesRegex(ValueError,"EXECUTION_ATTEMPT_LIMIT"):
            policy.Budget(self.path,"IMPLEMENTING",attempt=True)
    def test_policy_tampering_still_invalidates_authority(self):
        proof=policy.execution_context(self.path,self.f.inputs)["proof"]
        with patch.object(limits,"digest",return_value="0"*64):
            with self.assertRaisesRegex(ValueError,"EXECUTION_AUTHORITY_CHANGED"):
                policy.execution_context(self.path,self.f.inputs)
