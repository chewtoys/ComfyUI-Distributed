#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [[ -s "${NVM_DIR}/nvm.sh" ]]; then
    # shellcheck source=/dev/null
    . "${NVM_DIR}/nvm.sh"
fi

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    echo "[test-web] node/npm are not available." >&2
    echo "[test-web] Install nvm and Node, or ensure node/npm are on PATH." >&2
    exit 1
fi

if [[ -f "${REPO_ROOT}/.nvmrc" ]] && command -v nvm >/dev/null 2>&1; then
    nvm use >/dev/null
fi

cd "${REPO_ROOT}"

if [[ "${1:-}" == "--watch" ]]; then
    exec npx vitest web/tests
fi

exec npx vitest run web/tests
