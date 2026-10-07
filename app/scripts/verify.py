#!/usr/bin/env python3
"""One-shot verification service.

Aggregates four gating decisions and reports them as a single exit-status
bit-mask (0 == everything green):

    bit 0 (1)   application build (byte-compile + import)
    bit 1 (2)   code (unit) tests
    bit 2 (4)   HTTP smoke: a legal, fully signed file is ACCEPTed (200)
    bit 3 (8)   HTTP smoke: a checksum/datasum corruption is REJECTed (422)
    bit 4 (16)  HTTP smoke: a truncated file is REJECTed (422)

The service starts no HTTP server itself: in Docker Compose it waits for the
``web`` service to become healthy and then exits, which makes it usable as a
one-shot compose service.  For local development pass ``--spawn-server``.
"""

import argparse
import compileall
from contextlib import contextmanager
import importlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_APP_DIR = os.path.dirname(HERE)
if DEFAULT_APP_DIR not in sys.path:
    sys.path.insert(0, DEFAULT_APP_DIR)

from tests.fixtures import build_file, valid_image, valid_primary  # noqa: E402

EXIT_BUILD = 1 << 0
EXIT_TESTS = 1 << 1
EXIT_LEGAL = 1 << 2
EXIT_CORRUPT = 1 << 3
EXIT_TRUNCATED = 1 << 4


def step(name):
    print(f"\n=== {name} ===", flush=True)


def check_app_build(app_dir: str) -> bool:
    step("application build (compileall + import)")
    pkg_dir = os.path.join(app_dir, "fits_audit")
    ok = compileall.compile_dir(pkg_dir, quiet=1, maxlevels=5, force=True)
    if not ok:
        print("FAIL: byte-compilation reported errors")
        return False
    try:
        for mod in ("fits_audit", "fits_audit.server", "fits_audit.audit",
                    "fits_audit.checksum"):
            importlib.import_module(mod)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: cannot import {mod}: {exc!r}")
        return False
    print("PASS: application compiles and imports")
    return True


def check_unit_tests(app_dir: str) -> bool:
    step("code tests (unittest discover)")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests",
         "-p", "test_*.py", "-v"],
        cwd=app_dir,
    )
    if proc.returncode == 0:
        print("PASS: unit test suite")
    else:
        print(f"FAIL: unit tests (exit {proc.returncode})")
    return proc.returncode == 0


def wait_for_health(base_url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base_url.rstrip("/") + "/health",
                                        timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_err = exc
            time.sleep(0.5)
    print(f"FAIL: service did not become healthy: {last_err}")
    return False


def post_fits(base_url: str, payload: bytes):
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/fits/audit",
        data=payload,
        headers={"Content-Type": "application/fits"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def check_legal(base_url: str) -> bool:
    step("HTTP smoke: legal, fully signed file")
    hdus = [valid_primary(bitpix=16, axes=(3, 2),
                          values=[1, -2, 3, -4, 5, -6]),
            valid_image(bitpix=8, axes=(4,), values=[9, 8, 7, 6],
                        extname="SCI")]
    payload = build_file(hdus)
    status, doc = post_fits(base_url, payload)
    if status != 200 or doc.get("status") != "accepted":
        print(f"FAIL: expected HTTP 200/accepted, got {status}/{doc.get('status')}")
        print(json.dumps(doc.get("error"), indent=2))
        return False
    if len(doc.get("hdus", [])) != 2:
        print("FAIL: expected two HDU reports")
        return False
    for i, h in enumerate(doc["hdus"]):
        if h["index"] != i or h["byte_range"][1] <= h["byte_range"][0]:
            print(f"FAIL: malformed HDU report {h}")
            return False
        if h["datasum"]["status"] != "valid" or \
                h["checksum"]["status"] != "valid":
            print(f"FAIL: checksum verdicts not valid for HDU {i}")
            return False
    print("PASS: legal file accepted; 2 HDUs reported with valid sums")
    return True


def check_corrupt(base_url: str) -> bool:
    step("HTTP smoke: checksum/datasum corruption")
    payload = bytearray(build_file([
        valid_primary(bitpix=8, axes=(4,), values=[1, 2, 3, 4]),
        valid_image(bitpix=16, axes=(2,), values=[7, 8]),
    ]))
    # Flip a byte inside HDU 0's data block; DATASUM must fail first.
    payload[2880 + 1] ^= 0xFF
    status, doc = post_fits(base_url, bytes(payload))
    if status != 422 or doc.get("status") != "rejected":
        print(f"FAIL: expected HTTP 422/rejected, got {status}")
        return False
    err = doc.get("error") or {}
    if err.get("reason") not in ("DATASUM_MISMATCH", "CHECKSUM_MISMATCH"):
        print(f"FAIL: expected a *_MISMATCH reason, got {err.get('reason')}")
        return False
    if err.get("hdu") != 0 or not isinstance(err.get("offset"), int):
        print(f"FAIL: error must localise to HDU 0 with byte offset: {err}")
        return False
    print(f"PASS: corruption rejected ({err['reason']} at HDU 0, "
          f"byte {err['offset']})")
    return True


def check_truncated(base_url: str) -> bool:
    step("HTTP smoke: truncated file")
    full = build_file([
        valid_primary(bitpix=16, axes=(100,), values=list(range(100))),
        valid_image(bitpix=8, axes=(8,), values=list(range(8))),
    ])
    # Cut away the second half of the primary data block plus every later HDU.
    cut = 2880 + 100
    status, doc = post_fits(base_url, full[:cut])
    if status != 422 or doc.get("status") != "rejected":
        print(f"FAIL: expected HTTP 422/rejected, got {status}")
        return False
    err = doc.get("error") or {}
    if not str(err.get("reason", "")).startswith("TRUNCATED"):
        print(f"FAIL: expected a TRUNCATED_* reason, got {err.get('reason')}")
        return False
    if not isinstance(err.get("offset"), int):
        print("FAIL: truncation error must carry a byte offset")
        return False
    print(f"PASS: truncated file rejected ({err['reason']} at HDU "
          f"{err['hdu']}, byte {err['offset']})")
    return True


@contextmanager
def spawn_server(app_dir: str, port: int):
    proc = subprocess.Popen(
        [sys.executable, "-m", "fits_audit.server",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=app_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
        if out.strip():
            print("(server log)\n" + out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="FITS audit one-shot verifier")
    parser.add_argument("--base-url",
                        default=os.environ.get("VERIFY_BASE_URL"),
                        help="base URL of a running audit service")
    parser.add_argument("--app-dir", default=DEFAULT_APP_DIR)
    parser.add_argument("--spawn-server", action="store_true",
                        help="start a local server subprocess for the smoke "
                             "checks (local development)")
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument("--skip-smoke", action="store_true",
                        help="only run build and unit tests")
    args = parser.parse_args(argv)

    failures = 0
    if not check_app_build(args.app_dir):
        failures |= EXIT_BUILD
    if not check_unit_tests(args.app_dir):
        failures |= EXIT_TESTS

    if not args.skip_smoke:
        if args.spawn_server:
            with spawn_server(args.app_dir, args.port) as base_url:
                if not wait_for_health(base_url):
                    failures |= EXIT_LEGAL | EXIT_CORRUPT | EXIT_TRUNCATED
                else:
                    if not check_legal(base_url):
                        failures |= EXIT_LEGAL
                    if not check_corrupt(base_url):
                        failures |= EXIT_CORRUPT
                    if not check_truncated(base_url):
                        failures |= EXIT_TRUNCATED
        else:
            if not args.base_url:
                parser.error("--base-url is required unless --spawn-server "
                             "is given")
            if not wait_for_health(args.base_url):
                failures |= EXIT_LEGAL | EXIT_CORRUPT | EXIT_TRUNCATED
            else:
                if not check_legal(args.base_url):
                    failures |= EXIT_LEGAL
                if not check_corrupt(args.base_url):
                    failures |= EXIT_CORRUPT
                if not check_truncated(args.base_url):
                    failures |= EXIT_TRUNCATED

    print("\n=== summary ===")
    labels = [
        (EXIT_BUILD, "application build"),
        (EXIT_TESTS, "code tests"),
        (EXIT_LEGAL, "legal file HTTP verdict"),
        (EXIT_CORRUPT, "corrupted digest HTTP verdict"),
        (EXIT_TRUNCATED, "truncated file HTTP verdict"),
    ]
    for bit, label in labels:
        print(f"  [{'FAIL' if failures & bit else 'PASS'}] {label}")
    print(f"\nverify exit code: {failures}")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
