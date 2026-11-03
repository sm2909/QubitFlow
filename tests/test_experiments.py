import hashlib
import tempfile
from pathlib import Path
import unittest

import analyze_results
import run as experiments
from qf_policies import make_policy


class ExperimentDesignTests(unittest.TestCase):
    def test_policy_matrices_are_intentionally_separated(self):
        self.assertEqual(len(experiments.POLICIES), 13)
        self.assertEqual(len(experiments.ARCH_POLICIES), 4)
        self.assertTrue(
            {key for key, _ in experiments.ARCH_POLICIES}
            <= {key for key, _ in experiments.POLICIES}
        )

    def test_expected_group_matrix_counts(self):
        manifest = {
            "workloads": [{"tag": "one"}, {"tag": "two"}],
            "architecture_sweep": {
                "cr_slots": [2, 4],
                "n_banks": [1, 2],
                "line_access_cost": [0, 1],
            },
            "two_qubit_timing_sweep": [1, 2, 4],
            "policy_matrices": {
                "architecture_and_access_sensitivity": [
                    {"policy_name": make_policy(key, **params).name}
                    for key, params in experiments.ARCH_POLICIES
                ],
                "policy_ablation": [
                    {"policy_name": make_policy(key, **params).name}
                    for key, params in experiments.POLICIES
                ],
                "two_qubit_timing_sensitivity": [
                    {"policy_name": make_policy(key, **params).name}
                    for key, params in experiments.ARCH_POLICIES
                ],
            },
        }
        expected = analyze_results.expected_comparison_groups(manifest)
        # 2 workloads x (2x2x2 architecture + 1 ablation + 3 timing groups).
        self.assertEqual(len(expected), 2 * (8 + 1 + 3))
        arch_group = next(
            key for key in expected if ":architecture_and_access_sensitivity:" in key
        )
        ablation_group = next(key for key in expected if ":policy_ablation:" in key)
        timing_group = next(
            key for key in expected if ":two_qubit_timing_sensitivity:" in key
        )
        self.assertEqual(len(expected[arch_group]), 4)
        self.assertEqual(len(expected[ablation_group]), 13)
        self.assertEqual(len(expected[timing_group]), 4)

    def test_manifest_sha256_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.txt"
            path.write_text("controlled source", encoding="utf-8")
            good = hashlib.sha256(path.read_bytes()).hexdigest()
            analyze_results.validate_manifest_hashes({
                "code_and_input_sha256": {str(path): good}
            })
            with self.assertRaises(ValueError):
                analyze_results.validate_manifest_hashes({
                    "code_and_input_sha256": {str(path): "0" * 64}
                })


if __name__ == "__main__":
    unittest.main()
