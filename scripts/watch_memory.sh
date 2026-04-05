#!/bin/bash
set -euo pipefail
# 实时监控记忆系统 — 边说话边看效果
# 用法: 开一个终端跑这个脚本，另一个终端跑 python jarvis.py --no-wake

DB="${1:-data/memory/jarvis_memory.db}"
BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

clear
echo -e "${BLUE}═══════════════════════════════════════════${NC}"
echo -e "${BLUE}  小贾记忆系统实时监控${NC}"
echo -e "${BLUE}═══════════════════════════════════════════${NC}"
echo ""
echo "每 3 秒刷新一次。在另一个终端跑 python jarvis.py --no-wake 然后说话。"
echo "按 Ctrl+C 退出。"
echo ""

while true; do
    echo -e "\n${YELLOW}━━━ $(date '+%H:%M:%S') ━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    echo -e "\n${GREEN}【活跃记忆】${NC}"
    sqlite3 "$DB" "SELECT printf('  %-12s %-10s %-8s %s', category, COALESCE(key,'—'), importance, substr(content,1,40)) FROM memories WHERE active=1 ORDER BY created_at DESC LIMIT 8;" 2>/dev/null || echo "  (无)"

    echo -e "\n${GREEN}【已失活记忆】${NC}"
    sqlite3 "$DB" "SELECT printf('  %-12s %s', category, substr(content,1,40)) FROM memories WHERE active=0 ORDER BY updated_at DESC LIMIT 3;" 2>/dev/null || echo "  (无)"

    echo -e "\n${GREEN}【关系索引】${NC}"
    sqlite3 "$DB" "SELECT printf('  %s -[%s]-> %s', source_entity, relation, target_entity) FROM memory_relations ORDER BY created_at DESC LIMIT 5;" 2>/dev/null || echo "  (无)"

    echo -e "\n${GREEN}【最近 Episode】${NC}"
    sqlite3 "$DB" "SELECT printf('  %s [%s] %s', date, COALESCE(mood,'?'), substr(summary,1,40)) FROM episodes ORDER BY created_at DESC LIMIT 3;" 2>/dev/null || echo "  (无)"

    echo -e "\n${GREEN}【统计】${NC}"
    ACTIVE=$(sqlite3 "$DB" "SELECT count(*) FROM memories WHERE active=1;" 2>/dev/null || echo 0)
    INACTIVE=$(sqlite3 "$DB" "SELECT count(*) FROM memories WHERE active=0;" 2>/dev/null || echo 0)
    EPISODES=$(sqlite3 "$DB" "SELECT count(*) FROM episodes;" 2>/dev/null || echo 0)
    RELATIONS=$(sqlite3 "$DB" "SELECT count(*) FROM memory_relations;" 2>/dev/null || echo 0)
    echo "  活跃: $ACTIVE | 失活: $INACTIVE | Episode: $EPISODES | 关系: $RELATIONS"

    sleep 3
done
