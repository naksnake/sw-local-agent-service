"""`python -m slas_observability.render [observability/]` — write every provisioned file."""

from __future__ import annotations

import sys
from pathlib import Path

from slas_observability.provisioning import write_all


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else Path("observability")
    written = write_all(root)
    for path in written:
        print(path)
    print(f"{len(written)} files written under {root}.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
