#!/bin/bash
set -euo pipefail
# 实时监控记忆系统 — 边说话边看效果
# 用法: bash scripts/watch_memory.sh

DB="${1:-data/memory/jarvis_memory.db}"
B='\033[1;34m'   # bold blue
G='\033[0;32m'
Y='\033[1;33m'
D='\033[0;90m'   # dim
NC='\033[0m'

while true; do
    clear
    echo -e "${B}══════════════════════════════════════════════════════════${NC}"
    echo -e "${B}  🧠 小月记忆监控  $(date '+%H:%M:%S')${NC}"
    echo -e "${B}══════════════════════════════════════════════════════════${NC}"

    echo -e "\n${G}【活跃记忆】${NC}"
    sqlite3 -separator ' | ' "$DB" \
      "SELECT printf('  [%-10s] %-6s', category, importance) || ' ' || content
       FROM memories WHERE active=1
       ORDER BY updated_at DESC LIMIT 15;" 2>/dev/null || echo "  (无)"

    echo -e "\n${G}【用户画像】${NC}"
    sqlite3 "$DB" \
      "SELECT '  ' || key || ': ' || value
       FROM user_profiles
       ORDER BY updated_at DESC LIMIT 10;" 2>/dev/null || echo "  (无)"

    echo -e "\n${G}【最近 Episode】${NC}"
    sqlite3 "$DB" \
      "SELECT printf('  %s [%s] %s', date, COALESCE(mood,'—'), summary)
       FROM episodes ORDER BY created_at DESC LIMIT 5;" 2>/dev/null || echo "  (无)"

    echo -e "\n${G}【关系】${NC}"
    sqlite3 "$DB" \
      "SELECT printf('  %s —[%s]→ %s', source_entity, relation, target_entity)
       FROM memory_relations ORDER BY created_at DESC LIMIT 5;" 2>/dev/null || echo "  (无)"

    echo -e "\n${D}───────────────────────────────────────────────────────${NC}"
    ACTIVE=$(sqlite3 "$DB" "SELECT count(*) FROM memories WHERE active=1;" 2>/dev/null || echo 0)
    INACTIVE=$(sqlite3 "$DB" "SELECT count(*) FROM memories WHERE active=0;" 2>/dev/null || echo 0)
    EPISODES=$(sqlite3 "$DB" "SELECT count(*) FROM episodes;" 2>/dev/null || echo 0)
    RELATIONS=$(sqlite3 "$DB" "SELECT count(*) FROM memory_relations;" 2>/dev/null || echo 0)
    echo -e "  ${D}活跃 $ACTIVE | 失活 $INACTIVE | Episode $EPISODES | 关系 $RELATIONS${NC}"

    sleep 3
done
