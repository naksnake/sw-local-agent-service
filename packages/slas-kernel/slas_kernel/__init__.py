"""Agent Kernel — the shared lifecycle every agent runs (CLAUDE.md §5.1, ADR-0001).

`slas_kernel.kernel.Kernel.run()` takes an upload or an MES ticket through
ingest → ticket → plan → act → collect → rca → sop → close; `resume()` continues after a
crash from the journal. `slas_kernel.agent.Agent` is the one abstract class an agent
implements. `slas_kernel.null_agent.NullAgent` rehearses the lifecycle with five fake steps.

This module deliberately imports nothing: `slas_kernel.branding` is read by the host CLI
on a bare host (install.sh → slas doctor), where pydantic is not installed. Import the
kernel modules you need explicitly.
"""

__version__ = "0.0.1"
