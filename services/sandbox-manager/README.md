# services/sandbox-manager

Rootless Podman + gVisor sandboxes for the Coding Agent. No network, no credentials, no route
to any Git remote (INV-14). Never the platform host (INV-4).

| Module | What it does |
|---|---|
| `toolchains.py` | Standard library only. The offline bundle manifest (`Toolchains/manifest.json`), the language table (Python, C, C++, Rust, Shell, Go, TypeScript, YAML/JSON config), `resolve()` — newest bundled version, pinned version honoured if present, otherwise one sentence and a fallback — `detect_languages()`, and `add_toolchain()` behind `slas toolchain add`. |
| `spec.py` | `SandboxSpec` with the hardening gate: pinned image, no network, read-only rootfs, all capabilities dropped, no new privileges, pids/memory/CPU caps, exactly three mounts (project rw, scratch rw, identity ro), no secret-looking environment, no runtime socket, display, input device, SSH key or credential file. `podman_argv()` and `exec_argv()` produce argv only. gVisor first; hardened runc (user namespace + seccomp) as the quickstart fallback. |
| `runtime.py` | `SandboxRuntime` protocol and `FakeSandboxRuntime`. The Podman driver arrives with the socket and dependency decision. |
| `manager.py` | Sessions: per-user identity file (`user.name`, `<user>@slas.local`, no credential helper), project directory, TTL, quota per user, `exec` (argv only), `reap`. |
| `images.py` | Renders `images/sandbox-<language>/Dockerfile` for the newest bundled version of each language; a test keeps them in step. |
