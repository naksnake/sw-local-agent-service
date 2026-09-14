# slas-cli

The `slas` command line, mirroring the WebUI (CLAUDE.md §3). `doctor` lands in P0, `toolchain` in P6, `target` in P8 and `status` in P11 (`status.py`: services, GPUs, models, work, leases and open alerts as sentences or JSON, read-only through the same `Host` as `doctor`; exit code 1 when something needs a person).
