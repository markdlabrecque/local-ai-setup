#!/usr/bin/env python3
"""Download a pinned model artifact from a manifest, safely and resumably."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

CHUNK_SIZE = 1024 * 1024


def load_artifact(config: Path, quantization: str) -> dict:
    try:
        manifest = json.loads(config.read_text(encoding="utf-8"))
        artifacts = manifest["artifacts"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"invalid model manifest: {exc}") from exc
    matches = [a for a in artifacts if a.get("quantization") == quantization]
    if len(matches) != 1:
        raise RuntimeError(f"manifest must contain exactly one {quantization} artifact")
    artifact = matches[0]
    required = ("filename", "size_bytes", "sha256", "download_url")
    if any(key not in artifact for key in required):
        raise RuntimeError("manifest artifact is missing required identity fields")
    if not isinstance(artifact["size_bytes"], int) or artifact["size_bytes"] <= 0:
        raise RuntimeError("manifest artifact has invalid size")
    if len(artifact["sha256"]) != 64:
        raise RuntimeError("manifest artifact has invalid checksum")
    return artifact


def auth_headers() -> dict[str, str]:
    # Tokens are deliberately read only from the process environment.
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    port = parsed.port
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)
    return scheme, (parsed.hostname or "").lower(), port


class SameOriginRedirectHandler(HTTPRedirectHandler):
    """Do not leak the bearer token when a download redirects elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and _origin(req.full_url) != _origin(newurl):
            redirected.headers.pop("Authorization", None)
            redirected.unredirected_hdrs.pop("Authorization", None)
        return redirected


URL_OPENER = build_opener(SameOriginRedirectHandler)


def open_url(request: Request):
    return URL_OPENER.open(request, timeout=60)


@contextlib.contextmanager
def artifact_lock(destination: Path, filename: str):
    lock_path = destination / ("." + filename + ".lock")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def download(artifact: dict, destination: Path, min_free: int) -> bool:
    filename = Path(artifact["filename"]).name
    if filename != artifact["filename"] or filename in ("", ".", ".."):
        raise RuntimeError("manifest contains an unsafe filename")
    final = destination / filename
    partial = destination / ("." + filename + ".part")
    expected_size = artifact["size_bytes"]
    expected_hash = artifact["sha256"].lower()
    destination.mkdir(parents=True, exist_ok=True)

    with artifact_lock(destination, filename):
        return _download_locked(artifact, destination, final, partial, expected_size, expected_hash, min_free)


def _download_locked(artifact: dict, destination: Path, final: Path, partial: Path,
                     expected_size: int, expected_hash: str, min_free: int) -> bool:
    if final.exists():
        if final.is_file() and final.stat().st_size == expected_size and checksum(final) == expected_hash:
            print(f"already verified: {final}")
            return False
        # Never treat an unexpected or corrupt promoted file as usable.
        final.unlink()

    offset = partial.stat().st_size if partial.exists() else 0
    if offset == expected_size:
        if checksum(partial) == expected_hash:
            os.replace(partial, final)
            print(f"verified and installed: {final}")
            return True
        # A complete but corrupt partial cannot be resumed meaningfully.
        partial.unlink()
        offset = 0
    elif offset > expected_size:
        partial.unlink()
        offset = 0
    usage = shutil.disk_usage(destination)
    needed = expected_size - offset
    if usage.free < needed + min_free:
        raise RuntimeError(f"insufficient disk space: need {needed + min_free} bytes, have {usage.free}")

    headers = auth_headers()
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = Request(artifact["download_url"], headers=headers)
    try:
        response = open_url(request)
    except (HTTPError, URLError, OSError) as exc:
        raise RuntimeError(f"download failed: {exc}") from exc
    # A server ignoring Range would duplicate the prefix; restart safely.
    status = getattr(response, "status", None) or response.getcode()
    if offset and status != 206:
        offset = 0
        partial.unlink(missing_ok=True)
        headers.pop("Range", None)
        response.close()
        try:
            response = open_url(Request(artifact["download_url"], headers=headers))
        except (HTTPError, URLError, OSError) as exc:
            raise RuntimeError(f"download failed: {exc}") from exc
    remaining = expected_size - offset
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            response_size = int(content_length)
        except ValueError:
            response.close()
            partial.unlink(missing_ok=True)
            raise RuntimeError("download response has invalid Content-Length")
        if response_size > remaining:
            response.close()
            partial.unlink(missing_ok=True)
            raise RuntimeError("download response is larger than the expected artifact")

    with response:
        mode = "ab" if offset else "wb"
        received = 0
        with partial.open(mode) as stream:
            while received <= remaining:
                block = response.read(min(CHUNK_SIZE, remaining - received + 1))
                if not block:
                    break
                received += len(block)
                if received > remaining:
                    partial.unlink(missing_ok=True)
                    raise RuntimeError("download response is larger than the expected artifact")
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())

    if not partial.is_file() or partial.stat().st_size != expected_size:
        raise RuntimeError("download size mismatch; incomplete file retained for resume")
    if checksum(partial) != expected_hash:
        # Once complete, a checksum-invalid partial is not a safe resume base.
        partial.unlink()
        raise RuntimeError("checksum mismatch; refusing to promote downloaded file")
    os.replace(partial, final)
    print(f"verified and installed: {final}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/models.json"))
    parser.add_argument("--model-dir", type=Path, default=Path.home() / ".local/share/local-ai/models")
    parser.add_argument("--quantization", choices=("Q8_0", "Q6_K"), default="Q8_0")
    parser.add_argument("--min-free-bytes", type=int, default=0)
    args = parser.parse_args(argv)
    if args.min_free_bytes < 0:
        parser.error("--min-free-bytes must not be negative")
    try:
        artifact = load_artifact(args.config, args.quantization)
        download(artifact, args.model_dir, args.min_free_bytes)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
