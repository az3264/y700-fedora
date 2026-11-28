#!/usr/bin/python3
"""Behaviour tests for kshim-sign-boot.py.

The real avbtool.py and the real AOSP AVB test key that ships in the image are
used, so the signed image is byte-for-byte verifiable the way the bootloader
verifies boot_a. Only small synthetic payloads replace the real 96 MiB image.
No device, root, mount or raw block write is touched here.
"""
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
LIBEXEC = ROOT / "system_files/usr/libexec/armada"
AVBTOOL = ROOT / "packages/kshim-tools/vendor/avbtool.py"
KEY = ROOT / "system_files/usr/lib/armada/kshimc/testkey_rsa4096.pem"
SIGNER = LIBEXEC / "kshim-sign-boot.py"

PARTITION_SIZE = 8 * 1024 * 1024
ROLLBACK_INDEX = 1738713600
KEY_SHA1 = "2597c218aae470a130f61162feaae70afd97f011"

loader = importlib.machinery.SourceFileLoader("kshim_sign_boot_test",
                                              str(SIGNER))
spec = importlib.util.spec_from_loader(loader.name, loader)
signer = importlib.util.module_from_spec(spec)
loader.exec_module(signer)


def run(*args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = signer.main(list(args))
    return code, stdout.getvalue(), stderr.getvalue()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class SignTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.image = self.root / "boot.img"
        payload = b"ANDROID!" + b"\x00" * 4088 + b"gzip-kernel-payload"
        self.image.write_bytes(payload.ljust(4096, b"\x00"))
        # The repacker appends an unsigned AVB footer to the prepared image.
        code = subprocess.run([sys.executable, str(AVBTOOL), "add_hash_footer",
                               "--image", str(self.image),
                               "--partition_size", str(PARTITION_SIZE),
                               "--partition_name", "boot",
                               "--algorithm", "NONE"],
                              capture_output=True)
        self.assertEqual(code.returncode, 0, code.stdout + code.stderr)
        self.unsigned = self.root / "unsigned.img"
        self.unsigned.write_bytes(self.image.read_bytes())

    def sign(self, *extra):
        return run("--image", str(self.image), "--key", str(KEY),
                   "--avbtool", str(AVBTOOL),
                   "--partition-size", str(PARTITION_SIZE),
                   "--rollback-index", str(ROLLBACK_INDEX), *extra)

    def info(self, image):
        out = subprocess.run([sys.executable, str(AVBTOOL), "info_image",
                              "--image", str(image)],
                             capture_output=True, check=True).stdout.decode()
        return out

    def test_signs_and_verifies_with_the_shipped_key(self):
        code, out, err = self.sign()
        self.assertEqual(code, 0, err)
        self.assertIn("SHA256_RSA4096", out)
        self.assertIn(KEY_SHA1, out)
        text = self.info(self.image)
        self.assertRegex(text, r"Algorithm:\s+SHA256_RSA4096")
        self.assertIn("Public key (sha1):        %s" % KEY_SHA1, text)
        self.assertIn("Rollback Index:           %d" % ROLLBACK_INDEX, text)
        verify = subprocess.run([sys.executable, str(AVBTOOL), "verify_image",
                                 "--image", str(self.image),
                                 "--key", str(KEY)],
                                capture_output=True)
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
        self.assertEqual(self.image.stat().st_size, PARTITION_SIZE)

    def test_payload_bytes_and_footer_size_survive(self):
        before = signer.parse_footer(self.unsigned.read_bytes())
        self.sign()
        after = signer.parse_footer(self.image.read_bytes())
        self.assertEqual(before["original_image_size"],
                         after["original_image_size"])
        data = self.unsigned.read_bytes()[:before["original_image_size"]]
        self.assertEqual(self.image.read_bytes()[:after["original_image_size"]],
                         data)

    def test_check_mode_reports_the_signed_image(self):
        self.sign()
        code, out, err = run("--check", "--image", str(self.image),
                             "--key", str(KEY), "--avbtool", str(AVBTOOL),
                             "--expect-key-sha1", KEY_SHA1)
        self.assertEqual(code, 0, err)
        self.assertIn(KEY_SHA1, out)

    def test_check_mode_rejects_an_unsigned_image(self):
        code, out, err = run("--check", "--image", str(self.unsigned),
                             "--key", str(KEY), "--avbtool", str(AVBTOOL))
        self.assertEqual(code, 1)
        self.assertIn("no AVB signature", err)

    def test_refuses_a_key_that_does_not_match_the_expected_digest(self):
        code, out, err = self.sign("--expect-key-sha1", "0" * 40)
        self.assertEqual(code, 1)
        self.assertIn("does not match", err)
        self.assertEqual(sha256(self.image), sha256(self.unsigned))

    def test_refreshes_the_offline_report(self):
        report = self.root / "report.json"
        report.write_text(json.dumps({
            "layout_mode": "installed",
            "inputs": {"stock_boot": {"length": 1, "sha256": "0" * 64}},
            "outputs": {"final_boot": {"length": 0, "sha256": "0" * 64}},
            "checks": [],
        }), encoding="utf-8")
        code, out, err = self.sign("--report", str(report))
        self.assertEqual(code, 0, err)
        document = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(document["outputs"]["final_boot"]["sha256"],
                         sha256(self.image))
        self.assertEqual(document["outputs"]["final_boot"]["length"],
                         PARTITION_SIZE)
        self.assertEqual(document["avb_signing"]["algorithm"],
                         "SHA256_RSA4096")
        self.assertEqual(document["avb_signing"]["key_sha1"], KEY_SHA1)
        self.assertEqual(document["avb_signing"]["rollback_index"],
                         ROLLBACK_INDEX)
        self.assertEqual(document["inputs"]["signer"]["sha256"],
                         sha256(SIGNER))
        self.assertEqual(document["checks"][-1]["name"], "avb:signature")


if __name__ == "__main__":
    unittest.main()
