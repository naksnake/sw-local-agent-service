# slas-kernel

The Agent Kernel: the shared lifecycle every agent runs (CLAUDE.md §5.1, ADR-0001).

```
INGEST → TICKET → PLAN → (human approval for destructive steps) → ACT → COLLECT → RCA → SOP → CLOSE
```

| Module | Owns |
|---|---|
| `agent.py` | the `Agent` protocol — the only thing an agent implements |
| `kernel.py` | the lifecycle, approvals, crash recovery (`run`, `approve`, `resume`) |
| `journal.py` | the write-ahead journal: intent → observation per step (INV-6) |
| `executor.py` | the deterministic executor boundary (INV-3) and the `fake` primitive |
| `store.py` | ticket persistence: in memory, or `Tickets/<id>/ticket.json` |
| `logs.py` | COLLECT: per-step stdout/stderr into `Tickets/<id>/logs/` |
| `rca.py` | Phase 2 placeholder RCA with a deterministic fingerprint |
| `sop.py` | SOP model and the EN + zh-Hant renderings, always together (INV-13) |
| `null_agent.py` | five fake steps that rehearse the whole lifecycle |
| `branding.py` | the product name (CLAUDE.md §0.1) |

Agents never implement tickets, journals, logs, RCA, SOP or skills; those live here.
