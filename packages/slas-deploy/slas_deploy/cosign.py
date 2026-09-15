"""cosign verification for the prod profile (CLAUDE.md §3 prod column, ADR-0012).

Two places an image can come from, one key:

    bundle     the offline tarball: `cosign verify-blob` on `manifest.json` with the release
               public key, then every saved image's ID is checked against the manifest
    Harbor     the registry inside the perimeter: `cosign verify --key` on each reference by
               digest before `docker pull`; signatures live next to the images in Harbor

Key-based, offline: no Fulcio, no Rekor (`--insecure-ignore-tlog --private-infrastructure`
tells cosign there is no transparency log to consult, which is the truth in an air gap).
The public key ships in the repository as `config/cosign.pub`; the private key never does.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final

from slas_deploy.images import BundleManifest
from slas_hal.drivers.process import ProcessRunner
from slas_schemas.errors import ThreePartMessage

OFFLINE_FLAGS: Final[tuple[str, ...]] = ("--insecure-ignore-tlog", "--private-infrastructure")


class SignatureError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class CosignVerifier:
    def __init__(self, runner: ProcessRunner, *, public_key: Path) -> None:
        self.runner = runner
        self.public_key = public_key

    def _require_key(self) -> None:
        if not self.public_key.is_file():
            raise SignatureError(
                ThreePartMessage(
                    f"The release public key {self.public_key} is not on this host.",
                    "The prod profile verifies every image and the bundle manifest with it.",
                    "Copy config/cosign.pub from the release you are installing (its fingerprint "
                    "is in the release notes) and run ./install.sh again.",
                )
            )

    def verify_blob(self, blob: Path, signature: Path) -> None:
        self._require_key()
        result = self.runner.run(
            [
                "cosign",
                "verify-blob",
                "--key",
                str(self.public_key),
                "--signature",
                str(signature),
                *OFFLINE_FLAGS,
                str(blob),
            ],
            env={},
            timeout_s=120,
        )
        if result.exit_code != 0:
            raise SignatureError(
                ThreePartMessage(
                    f"The signature on {blob.name} does not verify.",
                    (result.stderr.strip().splitlines() or ["cosign gave no reason"])[-1],
                    "Nothing was installed. Get the bundle again from the release page and "
                    "compare the key fingerprint with the release notes before retrying.",
                )
            )

    def verify_image(self, reference: str) -> None:
        self._require_key()
        result = self.runner.run(
            ["cosign", "verify", "--key", str(self.public_key), *OFFLINE_FLAGS, reference],
            env={},
            timeout_s=300,
        )
        if result.exit_code != 0:
            raise SignatureError(
                ThreePartMessage(
                    f"{reference} is not signed by the release key.",
                    (result.stderr.strip().splitlines() or ["cosign gave no reason"])[-1],
                    "Nothing was pulled. Check that Harbor holds the release's signed images "
                    "(cosign artifacts next to them) and that the reference is by digest.",
                )
            )


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_bundle(
    bundle_dir: Path, verifier: CosignVerifier, *, manifest_name: str = "manifest.json"
) -> BundleManifest:
    """The manifest's signature, then every file the manifest lists by hash."""
    manifest_path = bundle_dir / manifest_name
    signature_path = bundle_dir / f"{manifest_name}.sig"
    for path in (manifest_path, signature_path):
        if not path.is_file():
            raise SignatureError(
                ThreePartMessage(
                    f"The bundle at {bundle_dir} has no {path.name}.",
                    "A release bundle carries manifest.json and its cosign signature.",
                    "Download the complete bundle; do not assemble one by hand.",
                )
            )
    verifier.verify_blob(manifest_path, signature_path)
    manifest = BundleManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    for relative, expected in manifest.files.items():
        path = bundle_dir / relative
        if not path.is_file() or sha256_of(path) != expected:
            raise SignatureError(
                ThreePartMessage(
                    f"{relative} in the bundle does not match the signed manifest.",
                    "The file is missing or was changed after the bundle was signed.",
                    "Nothing was installed. Download the bundle again.",
                )
            )
    return manifest


def write_manifest(bundle_dir: Path, version: str, built_at: str, images: dict[str, str]) -> Path:
    """What scripts/build-bundle.sh writes before signing (tests build tiny bundles with it)."""
    files = {
        str(path.relative_to(bundle_dir)): sha256_of(path)
        for path in sorted(bundle_dir.rglob("*"))
        if path.is_file() and path.name not in ("manifest.json", "manifest.json.sig")
    }
    manifest = BundleManifest(version=version, built_at=built_at, images=images, files=files)
    target = bundle_dir / "manifest.json"
    target.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )
    return target
