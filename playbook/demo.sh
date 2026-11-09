#!/usr/bin/env bash
# demo.sh — 60 秒看完全部核心行为
#
# 不用读文档。跑这一条，你会看到：
#   幂等、原子认领、隔离执行、证据门、篡改检测、审批门、外层退出码门
#
# 全部是真跑，输出是真结果。任一步失败会立即停下。

set -u
# git-bash 的 $PWD 是 /c/...，传给 Python 会变成 C:\c\...；用 pwd -W 拿原生路径
cd "$(dirname "$0")" || exit 1
cd starter || { echo "请在包根目录运行"; exit 1; }
HERE="$(pwd -W)"
export AGENT_BUS_DB="$HERE/demo.db"
export PYTHONIOENCODING=utf-8
rm -f demo.db demo.db-wal demo.db-shm bus.db bus.db-wal bus.db-shm

say() { printf '\n\033[1m=== %s\033[0m\n' "$1"; }
run() { printf '$ %s\n' "$*"; "$@"; }
jget() { python -c "import sys,json;d=json.load(sys.stdin);print($1)" | tr -d '\r'; }

python bus.py init >/dev/null

say "1. 幂等：同一条任务投两次，只有一条"
PAYLOAD=$(python -c "import json,sys;print(json.dumps({'target': sys.argv[1], 'ask': '统计规则条数'}, ensure_ascii=False))" "$HERE/RULES.md")
python bus.py submit --agent researcher --title "读规则并统计" \
    --payload "$PAYLOAD" --nonce demo-001 >/dev/null
OUT=$(python bus.py submit --agent researcher --title "读规则并统计" \
    --payload "$PAYLOAD" --nonce demo-001)
echo "$OUT" | jget "'duplicate =', d['duplicate']"
TID=$(echo "$OUT" | jget "d['task']['id']")
echo "task = $TID"

say "2. 没认领就想跑：执行器直接拒绝"
run python runner.py --id "$TID" --cmd 'python "x.py"' 2>&1 | tail -1

say "3. 认领 + 开始（此时才允许执行）"
python bus.py claim --agent researcher --task "$TID" >/dev/null
echo '$ python bus.py start --id '"$TID"
python bus.py start --id "$TID" | jget "'state =', d['state']"

say "4. 跑 agent：隔离目录 + 产出证据"
python runner.py --id "$TID" --cmd 'python "{ROOT}/demo/agent_research.py"' \
  | jget "'ok =', d['ok'], '| 上下文', d['context']['used_bytes'], 'B'"

say "5. 证据长什么样"
python -c "
import json,pathlib
v=json.loads(pathlib.Path(r'$HERE/runs/$TID/verification.json').read_text(encoding='utf-8'))
print('外层退出码 :', v['runlog']['exit_code'])
print('实际命令   :', v['runlog']['cmdline'][:64])
print('stdout sha :', v['runlog']['stdout_sha256'][:32], '...')
m = v.get('manifest')
if m:
    for c in m['commands'][1:3]:
        print('子命令     :', c['cmd'][:56], '| exit', c['exit_code'])
    print('产物       :', [a['path'] for a in m['artifacts']])
else:
    print('产物       : (本轮没有产出 manifest)')
    print('门拦下原因 :', v.get('problems'))
"

say "6. 篡改检测：改一个字节，门就过不去"
REPORT="$HERE/runs/$TID/work/report.md"
cp "$REPORT" "$HERE/_backup.md"
T2=$(python bus.py submit --agent researcher --title "篡改测试" --nonce demo-002 | jget "d['task']['id']")
python bus.py claim --agent researcher --task "$T2" >/dev/null
python bus.py start --id "$T2" >/dev/null
python bus.py evidence --id "$T2" --path "$REPORT" >/dev/null
printf 'x' >> "$REPORT"
run python bus.py done --id "$T2" --verdict "试试看" 2>&1 | tail -1
cp "$HERE/_backup.md" "$REPORT"; rm -f "$HERE/_backup.md"

say "7. 审批门：要外发的活，没人批就不许认领"
T3=$(python bus.py submit --agent researcher --title "发一封外部邮件" \
    --require-approval outbound --approval-reason "会对外发消息" --nonce demo-003 | jget "d['task']['id']")
echo "提交后状态 = $(python bus.py status --id "$T3" | jget "d['state']")"
echo "尝试认领   = $(python bus.py claim --agent researcher --task "$T3" | jget "d['claimed']")"
echo '$ python bus.py approve --id '"$T3"' --decision deny'
python bus.py approve --id "$T3" --decision deny | jget "'结果 =', d['state']"

say "8. 上下文：这张卡实际收到了多少"
python bus.py packet --id "$TID" | jget "'给它的:', d['used_bytes'], 'B  |  预算:', d['budget_bytes'], 'B  |  历史对比:', ('根任务无历史' if not d['comparable'] else str(d['full_history_bytes'])+' B')"

printf '\n\033[1m全部跑完。产物在 starter/runs/%s/\033[0m\n' "$TID"
printf '下一步：读 docs/避雷清单.md，看看这些门是怎么被踩出来的。\n\n'