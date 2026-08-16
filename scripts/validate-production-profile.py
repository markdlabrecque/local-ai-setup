#!/usr/bin/env python3
"""Validate the selected production profile against tracked live evidence."""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROFILE = ROOT / "config/production-profile.json"


def load(path: pathlib.Path):
    with path.open() as stream:
        return json.load(stream)


def digest(path: pathlib.Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> int:
    try:
        document = load(PROFILE)
        if document.get("schema_version") != 1 or document.get("selected_profile") != "quality-first":
            raise ValueError("unsupported production profile selection")
        profile = document["profiles"]["quality-first"]
        if profile.get("status") != "production":
            raise ValueError("quality-first is not marked production")
        model = profile["model"]
        if model != {"id": "Qwen3.5-27B-Q8_0", "quantization": "Q8_0",
                     "sha256": "6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450"}:
            raise ValueError("production model identity changed")
        if profile.get("runtime") != {"ref": "b10446", "commit": "adb55e5"}:
            raise ValueError("production runtime identity changed")

        for name, evidence in document["evidence"].items():
            path = (ROOT / evidence["path"]).resolve()
            if ROOT not in path.parents or not path.is_file() or digest(path) != evidence["sha256"]:
                raise ValueError(f"{name} evidence is missing or changed")

        tuning = load(ROOT / document["evidence"]["tuning"]["path"])
        benchmark = load(ROOT / document["evidence"]["benchmark"]["path"])
        pi_result = load(ROOT / document["evidence"]["pi"]["path"])
        candidate = tuning["stable_candidate"]
        if candidate.get("status") != "pass" or candidate["parameters"] != {
                "quantization": "Q8_0", "gpu_layers": 20, "flash_attention": "on",
                "batch": 256, "ubatch": 128, "kv_cache": "q8_0"}:
            raise ValueError("profile does not match the passing tuning candidate")
        if benchmark.get("summary") != {"passed_runs": 2, "status": "pass"}:
            raise ValueError("benchmark evidence is not a two-run pass")
        if pi_result.get("status") != "pass" or not all(row.get("passed") for row in pi_result.get("checks", [])):
            raise ValueError("Pi integration evidence is not passing")

        router = profile["router"]
        preset = load(ROOT / "config/router-presets.json")["presets"]["qwen3.5-27b-q8_0"]
        expected = {"context": router["context"], "device": router["device"],
                    "gpu_layers": router["gpu_layers"], "flash_attention": router["flash_attention"],
                    "batch": router["batch"], "ubatch": router["ubatch"],
                    "cache_k": router["cache_k"], "cache_v": router["cache_v"],
                    "autoload": router["autoload"]}
        if any(preset.get(key) != value for key, value in expected.items()):
            raise ValueError("router preset differs from selected profile")
        pi_model = load(ROOT / "config/pi-models.example.json")["providers"]["local-qwen"]["models"][0]
        pi_contract = profile["pi"]
        if (pi_model.get("id") != pi_contract["model"] or
                pi_model.get("contextWindow") != pi_contract["context_tokens"] or
                pi_model.get("maxTokens") != pi_contract["max_output_tokens"] or
                pi_model.get("compat", {}).get("thinkingFormat") != pi_contract["thinking_format"]):
            raise ValueError("Pi model metadata differs from selected profile")

        q6 = document["alternatives"]["q6_k_fallback"]
        interactive = document["alternatives"]["interactive"]
        if q6.get("status") != "not-selected" or interactive.get("status") != "not-selected":
            raise ValueError("an unvalidated alternative is selected")
        observed = profile["observed"]
        rows = {row["mode"]: row for row in benchmark["runs"]}
        for mode in ("cold", "warm"):
            metrics = rows[mode]["metrics"]
            expected_metrics = observed[mode]
            mapping = {"load_ms": "load_time_ms", "ttft_ms": "ttft_ms",
                       "prompt_tokens_per_second": "prompt_tokens_per_second",
                       "generation_tokens_per_second": "generation_tokens_per_second"}
            if any(expected_metrics[key] != metrics[value] for key, value in mapping.items()):
                raise ValueError(f"{mode} observed metrics differ from benchmark")
        print("production profile: valid (quality-first Q8_0, 20 Vulkan layers, 32K context)")
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
