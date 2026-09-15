#!/usr/bin/env bash
# slas-check <lint|type|build|test|validate> — runs the language's tool over /workspace.
# Shipped in every sandbox image; SLAS_LANGUAGE is set by the image. Argv only: the plan's
# steps call `slas-check <kind>` and never a shell line (CLAUDE.md §6.2, §11).
set -euo pipefail

kind="${1:-}"
lang="${SLAS_LANGUAGE:-}"
cd /workspace

skip() { echo "slas-check: $kind is not defined for $lang; nothing to run."; exit 0; }

case "$lang:$kind" in
  python:lint)      exec ruff check . ;;
  python:type)      exec mypy . ;;
  python:test)      exec pytest -q ;;
  c:build|cpp:build)
    if [ -f CMakeLists.txt ]; then cmake -S . -B build >/dev/null && exec cmake --build build; fi
    exec make ;;
  c:test|cpp:test)
    if [ -d build ] && [ -f CMakeLists.txt ]; then exec ctest --test-dir build --output-on-failure; fi
    exec make test ;;
  rust:lint)        exec cargo clippy -- -D warnings ;;
  rust:build)       exec cargo build ;;
  rust:test)        exec cargo test ;;
  shell:lint)       find . -name '*.sh' -not -path './.git/*' -print0 | xargs -0 -r shellcheck --shell=bash ;;
  shell:test)       if [ -d tests ]; then exec bats tests/; fi; skip ;;
  go:lint)          exec go vet ./... ;;
  go:build)         exec go build ./... ;;
  go:test)          exec go test ./... ;;
  typescript:type)  exec tsc --noEmit ;;
  typescript:test)  exec npm test ;;
  config:lint)      exec yamllint . ;;
  config:validate)
    find . \( -name '*.json' \) -not -path './.git/*' -print0 \
      | xargs -0 -r -n1 python3 -c 'import json,sys; json.load(open(sys.argv[1]))' ;;
  *) skip ;;
esac
