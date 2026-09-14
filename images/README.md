# images

Container image definitions. Every base image is pinned by digest (INV-8).

| Directory | What it is |
|---|---|
| `sandbox-<language>/` | One Coding Agent sandbox image per language (Python, C, C++, Rust, Shell, Go, TypeScript, YAML/JSON config). Rendered from `slas_sandbox_manager.images` for the newest bundled toolchain version; a unit test keeps the files in step. The toolchain is copied in from the offline bundle at build time; `git` is installed for local commits; there is no credential helper, no remote and no route out (INV-14). |
| `sandbox-common/slas-check.sh` | The `slas-check <lint\|type\|build\|test\|validate>` wrapper every plan step calls (argv only). |
| `screen-worker/` | Xvfb + x11vnc + noVNC + xdotool (P4; lives with its service). |
| `validation-executor/`, `factory-executor/` | P7 and P9. |

Building the sandbox images needs the bundle's `toolchains/<language>/<version>/` directory
next to the Dockerfile as build context, and an apt snapshot mirror for the pinned system
packages (the TODO in each Dockerfile).
