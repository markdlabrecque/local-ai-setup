"""Deterministic red contract for the issue #4 model downloader."""

import hashlib
import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
DOWNLOADER = ROOT / "scripts" / "download-model.py"


class RangeFixture(http.server.BaseHTTPRequestHandler):
    payload = b"fixture-q8-model" * 4096
    requests = []

    def do_GET(self):  # noqa: N802
        RangeFixture.requests.append(self.headers.get("Range"))
        start = 0
        value = self.headers.get("Range")
        if value:
            start = int(value.split("=", 1)[1].split("-", 1)[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(self.payload)-1}/{len(self.payload)}")
        else:
            self.send_response(200)
        body = self.payload[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class DownloaderContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = socketserver.TCPServer(("127.0.0.1", 0), RangeFixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def manifest(self, directory, *, sha256=None, size=None, url=None):
        artifact = {
            "id": "fixture-q8", "quantization": "Q8_0", "filename": "fixture.gguf",
            "size_bytes": size or len(RangeFixture.payload),
            "sha256": sha256 or hashlib.sha256(RangeFixture.payload).hexdigest(),
            "download_url": url or f"http://127.0.0.1:{self.server.server_address[1]}/fixture.gguf",
        }
        path = Path(directory) / "models.json"
        path.write_text(json.dumps({"schema_version": 1, "artifacts": [artifact]}))
        return path

    def run_downloader(self, config, model_dir, *extra):
        return subprocess.run(
            [sys.executable, str(DOWNLOADER), "--config", str(config), "--model-dir", str(model_dir), *extra],
            cwd=ROOT, text=True, capture_output=True,
            env={k: v for k, v in os.environ.items() if not k.startswith("HF_")},
        )

    def test_q8_default_consumes_manifest_and_resumes_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.manifest(tmp)
            model_dir = Path(tmp) / "models"
            model_dir.mkdir()
            partial = model_dir / ".fixture.gguf.part"
            partial.write_bytes(RangeFixture.payload[:100])
            result = self.run_downloader(config, model_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((model_dir / "fixture.gguf").read_bytes(), RangeFixture.payload)
            self.assertFalse(partial.exists())
            self.assertIn("bytes=100-", RangeFixture.requests)

    def test_verified_file_is_idempotent_and_destination_is_configurable(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.manifest(tmp)
            destination = Path(tmp) / "router-models"
            first = self.run_downloader(config, destination)
            self.assertEqual(first.returncode, 0, first.stderr)
            count = len(RangeFixture.requests)
            second = self.run_downloader(config, destination)
            self.assertEqual(first.returncode, second.returncode, second.stderr)
            self.assertEqual(len(RangeFixture.requests), count)
            self.assertEqual((destination / "fixture.gguf").read_bytes(), RangeFixture.payload)

    def test_checksum_corruption_is_rejected_without_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.manifest(tmp, sha256="0" * 64)
            destination = Path(tmp) / "models"
            result = self.run_downloader(config, destination)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("checksum", (result.stderr + result.stdout).lower())
            self.assertFalse((destination / "fixture.gguf").exists())

    def test_insufficient_space_is_rejected_before_http_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            RangeFixture.requests.clear()
            config = self.manifest(tmp)
            result = self.run_downloader(config, Path(tmp) / "models", "--min-free-bytes", str(10**18))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("space", (result.stderr + result.stdout).lower())
            self.assertEqual(RangeFixture.requests, [])

    def test_manifest_does_not_store_huggingface_token(self):
        text = (ROOT / "config" / "models.json").read_text().lower()
        self.assertNotIn("hf_token", text)
        self.assertNotIn("huggingface_token", text)
        self.assertNotIn("token=", text)


if __name__ == "__main__":
    unittest.main()
