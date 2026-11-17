#!/usr/bin/env bash
# Run agy headless with the prompt read from the file named by AGY_PROMPT_FILE.
# The prompt is passed as one argv element. It is never placed in a shell command string.
set -euo pipefail
exec agy -p "$(cat "${AGY_PROMPT_FILE:?AGY_PROMPT_FILE is required}")"
