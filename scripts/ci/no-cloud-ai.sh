#!/usr/bin/env bash
# INV-2: no cloud AI services. Fails when code or config references a cloud AI host or SDK.
# Documents (docs/, CLAUDE.md) may name them, since they say what is forbidden.
set -euo pipefail
cd "$(dirname "$0")/../.."

# Hosts and SDK imports of cloud AI services. HF_HUB_OFFLINE-style variables are allowed and
# do not match these patterns.
PATTERN='api\.openai\.com|api\.anthropic\.com|openai\.azure\.com|aiplatform\.googleapis\.com|generativelanguage\.googleapis\.com|bedrock-runtime|api-inference\.huggingface\.co|api\.smith\.langchain\.com|api\.wandb\.ai|^[[:space:]]*(import|from)[[:space:]]+(openai|anthropic|langsmith|wandb|google\.generativeai|boto3\.client\(.bedrock)|from[[:space:]]+\"(openai|@anthropic-ai/sdk)\"'

# Tracked and untracked (but not ignored) files, so the check also works before a commit.
files=$(git ls-files --cached --others --exclude-standard \
          apps services packages tests compose config scripts install.sh 2>/dev/null \
        | grep -v '^scripts/ci/no-cloud-ai.sh$' | sort -u || true)
if [ -z "$files" ]; then
  echo "no-cloud-ai: nothing to scan"
  exit 0
fi

hits=$(echo "$files" | xargs grep -nE "$PATTERN" 2>/dev/null || true)
if [ -n "$hits" ]; then
  echo "no-cloud-ai: cloud AI reference found (CLAUDE.md INV-2):" >&2
  echo "$hits" >&2
  exit 1
fi
echo "no-cloud-ai: clean ($(echo "$files" | wc -l | tr -d ' ') files scanned)"
