#!/usr/bin/env python3
"""Fetch model weights on a CONNECTED host and write checksums for the air-gapped box.

A thin wrapper over `packages/slas-fetch` (`slas_fetch`), which is standard library only, so
this script still runs on any host with Python 3.12 and no virtualenv: when the package is
not installed, the source tree's copy is put on `sys.path`. The running platform never
downloads anything (INV-1) except through the quickstart-only `model-fetcher` service, which
uses the same package (ADR-0018). Usage and the sources-file format: `slas_fetch.cli`.

    scripts/fetch_models.py fetch --sources config/model-sources.txt --profile quickstart --dest ./m
    scripts/fetch_models.py fetch --model qwen3.8-27b-fp8=Qwen/Qwen3.8-27B-FP8 --dest ./models
    scripts/fetch_models.py verify --dest /AI/Agent/Models [--model <path>]     # offline
    scripts/fetch_models.py merge-manifest --dest /AI/Agent/Models --from ./models/manifest.json
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import slas_fetch
except ModuleNotFoundError:  # a bare host: the source tree's package, no virtualenv
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "slas-fetch"))
    import slas_fetch

main = slas_fetch.main

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
