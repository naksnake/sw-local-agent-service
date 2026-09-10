"""The `slas` command line (CLAUDE.md §3).

P0 ships `slas doctor`, the preflight `install.sh` runs on a fresh host. This package uses
only the standard library on purpose: the preflight runs before uv, the venv or any
dependency exists on the host. Boundary models here are dataclasses mirroring
`slas_schemas`; a unit test keeps them in step.
"""

__version__ = "0.0.0"
