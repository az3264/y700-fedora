#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sign a prepared kShimC boot image with the fixed AOSP AVB test RSA4096 key.

`kshim-build-boot.py` repacks the stock Android boot image and deliberately
leaves the trailing AVB metadata unsigned (`Algorithm: NONE`). The Windows
installer signs that image before flashing it through EDL. A native in-place
update (see `armada-kshim-sync`) has to produce the very same authentication,
otherwise a locked bootloader rejects `boot_a` and the device does not boot.

This tool replicates exactly that step with the official avbtool:

  1. read the footer of the prepared image and keep only `original_image_size`
     bytes of boot data (header and gzip-packed kernel);
  2. run `avbtool add_hash_footer` with SHA256_RSA4096 over that data, the
     partition size and the recorded rollback index;
  3. verify the fresh signature against the public half of the signing key and
     re-read algorithm, key sha1 and rollback index from `info_image`;
  4. replace the prepared image atomically and, when asked, refresh the
     `outputs.final_boot` digest of the offline report.

The tool never touches a block device, never mounts anything, never reboots and
never reaches the network. The signing key is the public AOSP AVB test key that
Lenovo's TB322FC firmware already trusts (AVB public key sha1
2597c218aae470a130f61162feaae70afd97f011).
"""
import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile

BOOT_BYTES = 100663296
AVB_MAGIC = b"AVBf"
BOOT_MAGIC = b"ANDROID!"
DEFAULT_KEY = "/usr/lib/armada/kshimc/testkey_rsa4096.pem"
DEFAULT_AVBTOOL = "/usr/libexec/armada/kshim-tools/avbtool.py"
ALGORITHM = "SHA256_RSA4096"
EXPECTED_KEY_SHA1 = "2597c218aae470a130f61162feaae70afd97f011"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
PUBKEY_SHA1_LINE = re.compile(r"^\s*Public key \(sha1\):\s*([0-9a-f]{40})\s*$", re.M)
ALGORITHM_LINE = re.compile(r"^\s*Algorithm:\s*(\S+)\s*$", re.M)
ROLLBACK_INDEX_LINE = re.compile(r"^\s*Rollback Index:\s*([0-9]+)\s*$", re.M)


class SignError(Exception):
    pass


def need(condition, message):
    if not condition:
        raise SignError(message)


def checksum_bytes(data):
    return hashlib.sha256(data).hexdigest()


def file_sha256(path, label):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError as error:
        raise SignError("%s could not be read: %s" % (label, error))
    return digest.hexdigest()


def read_bytes(path, label):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as error:
        raise SignError("%s could not be read: %s" % (label, error))


def write_atomic(path, data):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle, temp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".",
                                    dir=directory)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp, 0o644)
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None and os.path.exists(temp):
            os.unlink(temp)


def parse_footer(image):
    need(len(image) > 64, "image is too small to carry an AVB footer")
    magic, major, minor, ois, vbo, vbs, _reserved = struct.unpack(
        ">4sIIQQQ28s", image[-64:])
    need(magic == AVB_MAGIC, "image has no AVB footer: %r" % (magic,))
    need(major == 1, "unsupported AVB footer version %d.%d" % (major, minor))
    need(ois > 4096, "AVB original_image_size is too small: %d" % ois)
    need(ois <= len(image) - 64,
         "AVB original_image_size %d exceeds the image length %d"
         % (ois, len(image)))
    need(vbs > 0 and vbo >= ois,
         "AVB vbmeta region is inconsistent: offset=%d size=%d" % (vbo, vbs))
    return {"original_image_size": ois, "vbmeta_offset": vbo,
            "vbmeta_size": vbs}


def run(command, name):
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, check=False)
    except OSError as error:
        raise SignError("%s could not run: %s" % (name, error))
    output = completed.stdout.decode("utf-8", "replace")
    if completed.returncode != 0:
        raise SignError("%s exited %d:\n%s"
                        % (name, completed.returncode, output.strip()))
    return output


def avbtool(avbtool_path, *rest, name="avbtool"):
    return run([sys.executable, "-B", avbtool_path] + list(rest), name)


def public_key_sha1(avbtool_path, key, workdir):
    blob = os.path.join(workdir, "signing-key.avbpub")
    avbtool(avbtool_path, "extract_public_key", "--key", key,
            "--output", blob, name="avbtool extract_public_key")
    digest = hashlib.sha1(read_bytes(blob, "AVB public key")).hexdigest()
    return blob, digest


def info_fields(avbtool_path, image_path):
    text = avbtool(avbtool_path, "info_image", "--image", image_path,
                   name="avbtool info_image")
    algorithm = ALGORITHM_LINE.search(text)
    key_sha1 = PUBKEY_SHA1_LINE.search(text)
    rollback = ROLLBACK_INDEX_LINE.search(text)
    need(algorithm is not None, "info_image printed no algorithm")
    return {"algorithm": algorithm.group(1),
            "key_sha1": key_sha1.group(1) if key_sha1 else None,
            "rollback_index": int(rollback.group(1)) if rollback else None}


def sign(image_path, key, avbtool_path, partition_size, partition_name,
         rollback_index, salt, expect_key_sha1):
    need(os.path.isfile(image_path), "image missing: %s" % image_path)
    need(os.path.isfile(key), "signing key missing: %s" % key)
    need(os.path.isfile(avbtool_path), "avbtool missing: %s" % avbtool_path)
    need(type(rollback_index) is int and rollback_index >= 0,
         "rollback index must be a non-negative integer: %r" % rollback_index)

    image = read_bytes(image_path, "prepared boot image")
    need(image[0:8] == BOOT_MAGIC, "prepared image is not an Android boot image")
    footer = parse_footer(image)
    ois = footer["original_image_size"]
    need(len(image) == partition_size,
         "prepared image length %d is not the partition size %d"
         % (len(image), partition_size))
    data = image[:ois]
    need(data[0:8] == BOOT_MAGIC, "prepared boot data lost its magic")

    if salt is None:
        # Deterministic salt: the image bytes already pin the content, and a
        # reproducible signature keeps the native update auditable.
        salt = hashlib.sha256(data).hexdigest()

    with tempfile.TemporaryDirectory(prefix="kshim-sign-") as workdir:
        _blob, key_sha1 = public_key_sha1(avbtool_path, key, workdir)
        if expect_key_sha1:
            need(key_sha1 == expect_key_sha1,
                 "signing key AVB sha1 %s does not match the expected %s"
                 % (key_sha1, expect_key_sha1))

        data_path = os.path.join(workdir, "boot.data")
        with open(data_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        signed_path = data_path
        avbtool(avbtool_path,
                "add_hash_footer",
                "--image", data_path,
                "--partition_size", str(partition_size),
                "--partition_name", partition_name,
                "--hash_algorithm", "sha256",
                "--algorithm", ALGORITHM,
                "--key", key,
                "--salt", salt,
                "--rollback_index", str(rollback_index),
                "--do_not_use_ab",
                name="avbtool add_hash_footer")
        signed = read_bytes(signed_path, "signed boot image")
        need(len(signed) == partition_size,
             "avbtool produced %d bytes, expected %d"
             % (len(signed), partition_size))
        need(signed[0:8] == BOOT_MAGIC, "signed image lost the Android magic")
        need(signed[:ois] == data, "avbtool changed the boot data region")
        signed_footer = parse_footer(signed)
        need(signed_footer["original_image_size"] == ois,
             "avbtool changed original_image_size")

        avbtool(avbtool_path, "verify_image", "--image", signed_path,
                "--key", key, name="avbtool verify_image")
        fields = info_fields(avbtool_path, signed_path)
        need(fields["algorithm"] == ALGORITHM,
             "signed image algorithm is %s" % fields["algorithm"])
        need(fields["key_sha1"] == key_sha1,
             "signed image key sha1 %s does not match %s"
             % (fields["key_sha1"], key_sha1))
        need(fields["rollback_index"] == rollback_index,
             "signed image rollback index %d does not match %d"
             % (fields["rollback_index"], rollback_index))

    write_atomic(image_path, signed)
    return {
        "image": image_path,
        "length": len(signed),
        "sha256": checksum_bytes(signed),
        "algorithm": ALGORITHM,
        "key_sha1": key_sha1,
        "rollback_index": rollback_index,
        "partition_name": partition_name,
        "partition_size": partition_size,
        "data_length": ois,
        "salt": salt,
    }


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def refresh_report(report_path, result):
    """Record the signed image in a kshim-build-boot.py offline report."""
    if not report_path:
        return None
    report = json.loads(read_bytes(report_path, "offline report").decode("utf-8"))
    need(isinstance(report, dict), "offline report is not an object")
    outputs = report.get("outputs")
    need(isinstance(outputs, dict), "offline report has no outputs")
    final = outputs.get("final_boot")
    need(isinstance(final, dict), "offline report has no final_boot output")
    final["length"] = result["length"]
    final["sha256"] = result["sha256"]
    final["avb_algorithm"] = result["algorithm"]
    final["avb_key_sha1"] = result["key_sha1"]
    final["avb_rollback_index"] = result["rollback_index"]
    checks = report.get("checks")
    need(isinstance(checks, list), "offline report has no checks")
    checks.append({
        "name": "avb:signature",
        "ok": True,
        "detail": "%s key sha1 %s rollback index %d"
                  % (result["algorithm"], result["key_sha1"],
                     result["rollback_index"]),
    })
    inputs = report.get("inputs")
    need(isinstance(inputs, dict), "offline report has no inputs")
    signer_path = os.path.realpath(__file__)
    inputs["signer"] = {
        "path": signer_path,
        "length": os.stat(signer_path).st_size,
        "sha256": sha256_file(signer_path),
    }
    report["avb_signing"] = {
        "algorithm": result["algorithm"],
        "key_sha1": result["key_sha1"],
        "rollback_index": result["rollback_index"],
        "partition_name": result["partition_name"],
        "partition_size": result["partition_size"],
        "salt": result["salt"],
    }
    write_atomic(report_path,
                 json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8"))
    return report_path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Sign a prepared kShimC boot image with the fixed AOSP AVB "
                    "test RSA4096 key and verify the result offline.")
    parser.add_argument("--image", required=True,
                        help="prepared boot image, signed in place")
    parser.add_argument("--key", default=DEFAULT_KEY,
                        help="AVB signing key (PEM)")
    parser.add_argument("--avbtool", default=DEFAULT_AVBTOOL)
    parser.add_argument("--partition-size", type=int, default=BOOT_BYTES)
    parser.add_argument("--partition-name", default="boot")
    parser.add_argument("--rollback-index", type=int, default=None,
                        help="AVB rollback index; required when signing")
    parser.add_argument("--salt", default=None,
                        help="AVB salt (hex); defaults to the data sha256")
    parser.add_argument("--expect-key-sha1", default=EXPECTED_KEY_SHA1,
                        help="refuse to sign when the key digest differs")
    parser.add_argument("--allow-any-key", action="store_true",
                        help="skip the fixed AOSP key digest check")
    parser.add_argument("--report", default=None,
                        help="offline report whose final_boot digest is refreshed")
    parser.add_argument("--check", action="store_true",
                        help="only verify an already signed image")
    return parser.parse_args(argv)


def check_only(args):
    need(os.path.isfile(args.image), "image missing: %s" % args.image)
    image = read_bytes(args.image, "boot image")
    footer = parse_footer(image)
    fields = info_fields(args.avbtool, args.image)
    need(fields["algorithm"] != "NONE",
         "image carries no AVB signature (Algorithm: NONE)")
    need(args.allow_any_key or not args.expect_key_sha1
         or fields["key_sha1"] == args.expect_key_sha1,
         "signed image key sha1 %s does not match the expected %s"
         % (fields["key_sha1"], args.expect_key_sha1))
    result = {"image": args.image, "length": len(image),
              "sha256": checksum_bytes(image)}
    result.update(fields)
    result["data_length"] = footer["original_image_size"]
    return result


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.check:
            result = check_only(args)
        else:
            need(args.rollback_index is not None,
                 "--rollback-index is required when signing")
            need(args.rollback_index >= 0,
                 "--rollback-index must not be negative")
            expect = None if args.allow_any_key else args.expect_key_sha1
            result = sign(args.image, args.key, args.avbtool,
                          args.partition_size, args.partition_name,
                          args.rollback_index, args.salt, expect)
            refresh_report(args.report, result)
        sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
        return 0
    except SignError as error:
        sys.stderr.write("kshim-sign-boot: %s\n" % error)
        return 1
    except OSError as error:
        sys.stderr.write("kshim-sign-boot: %s\n" % error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
