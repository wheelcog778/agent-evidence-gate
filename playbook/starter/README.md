# starter：最小任务总线

30 项测试全过的可运行版本。纯 Python 标准库，无依赖。

## 三步跑起来

```bash
python bus.py init
python selftest.py       # 期望 30/30 PASS
```

然后手工走一张卡：

```bash
python bus.py submit --agent researcher --title "读规则" \
    --payload '{"target":"RULES.md","ask":"统计规则条数"}' --nonce demo-001

python bus.py claim --agent researcher      # 返回 claimed.id
python bus.py start --id <id>
python runner.py --id <id> --cmd 'python "{ROOT}/demo/agent_research.py"'
python bus.py status --id <id>
```

产物在 `runs/<id>/work/`，含 `report.md` 和 `manifest.json`；核验记录在 `runs/<id>/verification.json`。

## 文件

| 文件 | 作用 |
|---|---|
| `bus.py` | 状态机、租约、证据门、审批门、CLI |
| `context.py` | 四槽预算上下文（task / upstream / brief / rules） |
| `runner.py` | 隔离目录执行 + manifest 校验 |
| `bridge_handoff.py` | 五列交接，写 `inbox/` |
| `selftest.py` | 30 项断言 |
| `demo/agent_research.py` | 确定性 agent：读文件 → 出报告 + 可复现命令 |
| `demo/agent_verify.py` | 独立核验 agent：重算 hash、核章节 |
| `profiles/` | 岗位说明，进上下文 |

## 命令速查

| 命令 | 作用 |
|---|---|
| `submit --nonce` | 幂等提交；带 `--parent` 建依赖；`--require-approval` 直接阻塞 |
| `claim --lease` | 原子认领 |
| `start` / `done` / `fail` / `block` | 状态流转（有前置条件） |
| `evidence --path` | 登记证据（必须是普通文件） |
| `approve --decision` | 人工审批 |
| `reap` | 回收过期租约、超期任务 |
| `packet --id` | 看这个 agent 实际收到的上下文 |
| `handoff` | 写五列交接 |

## 接你自己的模型

看 `demo/agent_verify.py`：它读 `AGENT_BUS_PACKET`（上下文）、写 `work/manifest.json`（交付清单）。
把你的模型调用套进这个形状就行，不需要改总线。

manifest 的最小形状：

```json
{
  "artifacts": [{"path": "out.md", "sha256": "...", "bytes": 123, "produced_by": 0}],
  "commands": [{"cmd": "python run.py", "exit_code": 0,
                "stdout_path": "logs/run.log", "stdout_sha256": "..."}],
  "output_summary": "一句话",
  "verdict": "一句话结论"
}
```

## 注意

- `runner.py` 只跑状态为 `running` 的任务。没 claim/start 就调，会被拒。
- 外层进程退出码非 0 直接判失败，不进入 manifest 校验。
- 产物路径必须在自己的 `work/` 里，`../` 或绝对路径会被拒。
- 不要改 `selftest.py` 的断言去迁就实现。测试松了，它会替你撒谎。