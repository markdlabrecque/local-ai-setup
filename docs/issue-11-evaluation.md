# Issue #11 evaluation suite

The suite is a version-controlled, deterministic smoke/evaluation gate for an
OpenAI-compatible chat endpoint. It does not require the 28 GB model: the
committed endpoint fixture is sufficient for the portable gate.

```bash
python3 tests/fixtures/fake_openai_endpoint.py --port 8089
scripts/run-evaluation.sh \
  --endpoint http://127.0.0.1:8089 \
  --cases tests/fixtures/evaluation_cases.json \
  --workspace /path/to/disposable-repo \
  --artifacts results/evaluation.json
```

`run-evaluation.sh` copies regular files into a temporary sandbox, excludes
`.git` and symlinks, and removes the sandbox at exit. It never applies a patch
to the supplied workspace. Command execution is an explicit allowlist fixture
(`printf EVAL_COMMAND_OK`); model-provided commands and paths are not run.
Requests use temperature zero, a fixed seed, and stable JSON serialization.
Long-context input is generated near the 32K target but is not retained in the
report. Captures, transcripts, and strings are bounded and sanitized for paths,
credentials, bearer tokens, GitHub tokens, and private keys.

The manifest covers chat, retrieval, code generation, navigation, disposable
patching, command checks, malformed/sequential/parallel tool calls and replay,
reasoning on/off, cancellation/recovery, overflow/compaction, and provenance.
Reports use [`schemas/evaluation-report.schema.json`](../schemas/evaluation-report.schema.json)
and contain per-case scores, a mean score, bounded provenance, lifecycle
summaries, and safety assertions. A non-zero exit means at least one required
case failed.
