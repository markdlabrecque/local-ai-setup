"""Executable contract for issue #3 model artifacts and memory budget."""

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "config" / "models.json"
BUDGET = ROOT / "config" / "memory-budget.json"


class ModelArtifactManifestTests(unittest.TestCase):
    def test_manifest_pins_primary_and_fallback_with_provenance(self):
        self.assertTrue(MANIFEST.is_file(), f"missing tracked manifest: {MANIFEST}")
        manifest = json.loads(MANIFEST.read_text())
        self.assertEqual(manifest["schema_version"], 1)
        artifacts = manifest["artifacts"]
        self.assertEqual({a["quantization"] for a in artifacts}, {"Q8_0", "Q6_K"})

        for artifact in artifacts:
            self.assertRegex(artifact["filename"], r"Qwen.*27B.*\.gguf$")
            self.assertRegex(artifact["revision"], r"^[0-9a-f]{40}$")
            self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(artifact["size_bytes"], 0)
            self.assertTrue(artifact["repository"])
            self.assertTrue(artifact["source_model"])
            self.assertTrue(artifact["download_url"].startswith("https://huggingface.co/"))


class MemoryBudgetTests(unittest.TestCase):
    def test_budget_proves_32k_safety_and_rejects_bf16(self):
        self.assertTrue(BUDGET.is_file(), f"missing machine-readable budget: {BUDGET}")
        budget = json.loads(BUDGET.read_text())
        self.assertEqual(budget["context_tokens"], 32768)
        self.assertGreaterEqual(budget["host"]["ram_gib"], 60)
        self.assertGreaterEqual(budget["host"]["vram_gib"], 16)

        available = budget["host"]["ram_gib"] - budget["host"]["os_reserve_gib"]
        q8 = budget["profiles"]["Q8_0"]
        q6 = budget["profiles"]["Q6_K"]
        for profile in (q8, q6):
            self.assertGreater(profile["weights_gib"], 0)
            self.assertGreater(profile["runtime_gib"], 0)
            self.assertGreater(profile["kv_cache_gib"], 0)
            self.assertLessEqual(
                profile["weights_gib"] + profile["runtime_gib"] + profile["kv_cache_gib"],
                available * profile["ram_safety_fraction"],
            )
        bf16 = budget["profiles"]["BF16"]
        self.assertGreater(
            bf16["weights_gib"] + bf16["runtime_gib"] + bf16["kv_cache_gib"],
            available * bf16["ram_safety_fraction"],
        )


if __name__ == "__main__":
    unittest.main()
