"""`slas doctor` — the host preflight behind `./install.sh` (CLAUDE.md §3).

Layout:
- `host.py`   how the checks look at the machine (`Host` protocol, `RealHost`)
- `fakes.py`  a scripted `FakeHost` for tests; nothing in the tests touches the real host
- `checks.py` the checks themselves, each a pure function of a `Host`
- `report.py` plain-language text and JSON rendering, summary sentence, exit code
"""

from slas_cli.doctor.checks import (
    ALL_CHECKS,
    CheckResult,
    DoctorSettings,
    describe_host,
    run_checks,
)
from slas_cli.doctor.host import CommandResult, Host, RealHost
from slas_cli.doctor.report import exit_code, render_json, render_text, summarize

__all__ = [
    "ALL_CHECKS",
    "CheckResult",
    "CommandResult",
    "DoctorSettings",
    "Host",
    "RealHost",
    "describe_host",
    "exit_code",
    "render_json",
    "render_text",
    "run_checks",
    "summarize",
]
