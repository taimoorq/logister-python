"""Exercise the actual publication-verification shell with registry fixtures."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RegistryFixture(unittest.TestCase):
    def verify(self, metadata, status=200, changed_download=False, empty=False, sidecars=False):
        text = (ROOT / WORKFLOW).read_text()
        block = text.split("      - name: " + STEP + "\n", 1)[1]
        block = block.split("        run: |\n", 1)[1].split("\n      - name:", 1)[0].split("\n  github-release:", 1)[0]
        script = textwrap.dedent(block)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / "bin"
            binary.mkdir()
            (root / "pkg").mkdir()
            (root / "dist").mkdir()
            if not empty:
                for name, body in ARTIFACTS.items():
                    (root / name).write_bytes(body)
            if sidecars:
                (root / "dist/package.whl.publish.attestation").write_text("signed attestation")
            fixture = {"metadata": metadata, "status": status, "payload": "different" if changed_download else "tested gem"}
            (root / "fixture.json").write_text(json.dumps(fixture))
            curl = binary / "curl"
            curl.write_text("#!/usr/bin/env python3\n" + textwrap.dedent('''
                import json, os, sys
                from pathlib import Path
                args = sys.argv[1:]
                fixture = json.loads(Path(os.environ["REGISTRY_FIXTURE"]).read_text())
                url = next(a for a in args if a.startswith("https://"))
                is_metadata = url.endswith("json")
                status = fixture["status"] if is_metadata else 200
                if status >= 400 and "--fail" in args:
                    sys.exit(22)
                body = json.dumps(fixture["metadata"]) if is_metadata else fixture["payload"]
                if "--output" in args:
                    Path(args[args.index("--output") + 1]).write_text(body)
                else:
                    print(body)
                if "--write-out" in args:
                    print(status, end="")
            '''))
            curl.chmod(0o755)
            sleeper = binary / "sleep"
            sleeper.write_text("#!/bin/sh\nexit 0\n")
            sleeper.chmod(0o755)
            env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                   "REGISTRY_FIXTURE": str(root / "fixture.json"), "RELEASE_TAG": TAG, "TAG_NAME": TAG}
            return subprocess.run(["bash", "-c", script], cwd=root, env=env,
                                  capture_output=True, text=True, timeout=15)

WORKFLOW = ".github/workflows/publish.yml"
STEP = "Verify PyPI publication"
TAG = "v0.4.0"
ARTIFACTS = {"dist/package.whl": b"tested wheel", "dist/package.tar.gz": b"tested sdist"}


def metadata():
    return {"urls": [{"filename": Path(name).name, "digests": {"sha256": hashlib.sha256(body).hexdigest()}}
                     for name, body in ARTIFACTS.items()]}


class PublicationVerificationTest(RegistryFixture):
    def test_attestation_sidecars(self):
        result = self.verify(metadata(), sidecars=True)
        self.assertEqual(result.returncode, 0, result.stderr)
    def test_missing_remote_distribution(self):
        self.assertNotEqual(self.verify({"urls": metadata()["urls"][:1]}).returncode, 0)
    def test_wrong_hash(self):
        data = metadata()
        data["urls"][0]["digests"]["sha256"] = "0" * 64
        self.assertNotEqual(self.verify(data).returncode, 0)
    def test_empty_dist(self):
        self.assertNotEqual(self.verify(metadata(), empty=True, sidecars=True).returncode, 0)
    def test_provider_failure(self):
        self.assertNotEqual(self.verify(metadata(), status=503).returncode, 0)


if __name__ == "__main__":
    unittest.main()
