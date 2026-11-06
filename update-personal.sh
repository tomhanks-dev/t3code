#!/bin/bash
set -euo pipefail
project_dir="$(cd "$(dirname "$0")" && pwd)"
git -C "$project_dir" fetch origin refs/heads/dev:refs/remotes/origin/dev
commit="$(git -C "$project_dir" rev-parse origin/dev)"
exec python3 "$project_dir/scripts/personal-fork/manage.py" stage "$commit"
