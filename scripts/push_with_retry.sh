#!/usr/bin/env bash

set -u

remote="${1:-origin}"
branch="${2:-main}"
max_attempts="${PUSH_MAX_ATTEMPTS:-4}"
base_delay="${PUSH_RETRY_DELAY_SECONDS:-5}"

for ((attempt = 1; attempt <= max_attempts; attempt++)); do
  if git push "$remote" "$branch"; then
    exit 0
  fi

  if ((attempt == max_attempts)); then
    echo "::error::git push to ${remote}/${branch} failed after ${max_attempts} attempts."
    exit 1
  fi

  delay=$((base_delay * attempt))
  echo "::warning::git push attempt ${attempt}/${max_attempts} failed; retrying in ${delay}s."
  sleep "$delay"
done
