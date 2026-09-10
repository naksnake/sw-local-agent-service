"""`python -m slas_cli` entry point, used by install.sh before the CLI is installed."""

from slas_cli.cli import main

raise SystemExit(main())
