#!/usr/bin/env bash
# Run next to the source deployment's Compose file; only Docker is required on the host.
set -euo pipefail
helper_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
compose_file="${WENYI_COMPOSE_FILE:-docker-compose.yml}"
if [[ "${1:-}" == "--compose" ]]; then compose_file="$2"; shift 2; fi
command="${1:-list}"
if [[ $# -gt 0 ]]; then shift; fi
helper="$helper_dir/wenyi-transfer.pyz"
if [[ ! -f "$helper" ]]; then
  echo "Missing wenyi-transfer.pyz. Use the migration tools from the Windows release ZIP." >&2
  exit 1
fi
docker_args=(compose -f "$compose_file")
remote="/tmp/wenyi-transfer-$(date +%s)-$$"
docker "${docker_args[@]}" exec -T api mkdir "$remote"
trap 'docker "${docker_args[@]}" exec -T api rm -rf -- "$remote" >/dev/null 2>&1 || true' EXIT
docker "${docker_args[@]}" cp "$helper" "api:$remote/helper.pyz"
case "$command" in
  list) docker "${docker_args[@]}" exec -T api python "$remote/helper.pyz" list "$@" ;;
  export)
    if [[ $# -lt 2 ]]; then echo "Usage: $0 [--compose FILE] export OUTPUT.wenyi.zip PROJECT_ID [...]" >&2; exit 2; fi
    output="$1"; shift
    if [[ -e "$output" ]]; then echo "Output already exists: $output" >&2; exit 2; fi
    selected=(); for project in "$@"; do selected+=(--project "$project"); done
    docker "${docker_args[@]}" exec -T api python "$remote/helper.pyz" export --output "$remote/projects.wenyi.zip" "${selected[@]}"
    docker "${docker_args[@]}" cp "api:$remote/projects.wenyi.zip" "$output"
    ;;
  *) echo "Expected list or export" >&2; exit 2 ;;
esac
