# services/sandbox-manager

Rootless Podman/Docker + gVisor sandboxes for the Coding Agent. No network, no credentials, no
route to any Git remote (INV-14). Never the platform host (INV-4). The HTTP surface is
`docs/api-contract-round-2.md` §4; the container command is `slas-sandbox-manager serve`.

| Module | What it does |
|---|---|
| `toolchains.py` | Standard library only. The toolchain manifest (`Toolchains/manifest.json`), the language table (Python, C, C++, Rust, Shell, Go, TypeScript, YAML/JSON config), `resolve()` — newest version, pinned version honoured if present, otherwise one sentence and a fallback — `detect_languages()`, `add_toolchain()` behind `slas toolchain add`, and `image_for()`: `<registry>/slas/sandbox-<language>:<version>` with the registry from `SLAS_SANDBOX_REGISTRY` (default `local`, what `install.sh --build` tags with). |
| `spec.py` | `SandboxSpec` with the hardening gate: pinned image, no network, read-only rootfs, all capabilities dropped, no new privileges, pids/memory/CPU caps, exactly three mounts (project rw, scratch rw, identity ro), no secret-looking environment, no runtime socket, display, input device, SSH key or credential file. `podman_argv()` and `exec_argv()` produce argv only. gVisor first; hardened runc (seccomp) as the quickstart fallback. |
| `runtime.py` | `SandboxRuntime` protocol, `FakeSandboxRuntime`, and `ContainerApiRuntime`: the Engine API driver over `slas_container.ContainerApi` that carries the same hardening as `podman_argv` (network none, read-only rootfs, tmpfs `/tmp`, cap drop ALL, no-new-privileges, pids/memory/CPU limits, uid 10001, `/workspace`, runtime `runsc`/`runc`, seccomp as `SecurityOpt` for runc) and translates mount sources from the manager's `/data` to `${SLAS_HOST_DATA_ROOT}`. `detect_isolation()` starts and removes a throwaway container under `runsc` at start-up and falls back to hardened runc with one warning sentence. |
| `manager.py` | Sessions: per-user identity file (`user.name`, `<user>@slas.local`, no credential helper), project directory, TTL, quota per user, `exec` (argv only), `reap`. |
| `terminal.py` | The Terminal tab's line mode: each line runs inside the sandbox; the transcript is redacted; `git push` is answered with `PUSH_EXPLANATION`. |
| `images.py` | Standard library only. The connected-build recipes (pinned upstream image by digest or pinned package per language), the offline-bundle variant behind `--source bundle`, and the CLI `install.sh --build` calls: `python -m slas_sandbox_manager.images list` (`name<TAB>tag<TAB>dockerfile`), `… manifest --out <path>` (the toolchain manifest the images satisfy), `… render` (writes `images/sandbox-*/Dockerfile*`; a test keeps them in step). |
| `service/settings.py` | Settings from the environment (`SLAS_RUNTIME_SOCKET`, `SLAS_DATA_ROOT`, `SLAS_HOST_DATA_ROOT`, `DEFAULT_RUNTIME`, `SANDBOX_TIER`, `SLAS_SANDBOX_REGISTRY`, `SLAS_TOOLCHAIN_MANIFEST`, …; the table is in the module docstring). |
| `service/app.py` | `build_services()` (every collaborator injectable), `create_app()`, the isolation probe, the reaper thread (every 60 s), `/health` → `{"runtime": "ok", "isolation": "gvisor"\|"runc"}` or a three-part 503 naming `runtime`. |
| `service/routes.py` | `/v1/sessions` (open, list by `user`/`slug`, get, exec, close, terminal with `git:terminal`), `/v1/toolchains`, `/v1/toolchains/resolve`, `/v1/languages/detect`, `/v1/reap`. |
| `cli.py` | `slas-sandbox-manager serve`. |
