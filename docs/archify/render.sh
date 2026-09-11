#!/usr/bin/env bash
set -euo pipefail

diagram_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
skill_dir="${ARCHIFY_SKILL_DIR:-${HOME}/.agents/skills/archify}"
archify_cli="${skill_dir}/bin/archify.mjs"

if [[ ! -f "$archify_cli" ]]; then
  echo "Archify 未安装：请安装 tt-a1i/archify，或设置 ARCHIFY_SKILL_DIR。" >&2
  exit 1
fi
if [[ $# -gt 1 || ($# -eq 1 && "$1" != "--visual-check") ]]; then
  echo "用法：bash docs/archify/render.sh [--visual-check]" >&2
  exit 2
fi

for entry in "architecture:jarvis.architecture" "sequence:task-run.sequence"; do
  diagram_type="${entry%%:*}"
  diagram_name="${entry#*:}"
  diagram_source="${diagram_dir}/${diagram_name}.json"
  diagram_output="${diagram_dir}/${diagram_name}.html"

  node "$archify_cli" validate "$diagram_type" "$diagram_source" --quality showcase --json > /dev/null
  node "$archify_cli" deliver "$diagram_type" "$diagram_source" "$diagram_output" --quality showcase --json > "${diagram_dir}/${diagram_name}.delivery.json"
  echo "已生成：${diagram_output}"
  if [[ "${1:-}" == "--visual-check" ]]; then
    node "$archify_cli" visual-check "$diagram_output" --json
  fi
done
