"""Allow `python -m slas_cli …`, which is how install.sh calls the preflight."""

import sys

from slas_cli.cli import main

if __name__ == "__main__":
    sys.exit(main())
