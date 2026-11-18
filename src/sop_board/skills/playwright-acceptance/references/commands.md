# 命令

在运行目录执行，先 `set -a; . ./run.env; set +a`。

```bash
# 执行（环境变量见 templates/fixtures.mjs 顶部注释；PW_OUT、PW_SCOPE 等已在 run.env）
npx --no-install playwright test

# 转成结果契约；blocked 与 skip 用 --finding 写原因；not-executed 的原因取 notExecuted() 的文字（--finding 可覆盖）；超过 64 MiB 的证据加 --skip-large
sop-board playwright convert --report playwright/report.json --out . --executor <会话> \
  --scope scope.json --scenario-file scenarios.yaml [--finding shop#W=<原因>] [--allow-partial] [--skip-large]

# 按运行目录 prompt.md 补原因类别，合并其他入口的结果，再逐条裁决
sop-board run verdict <key> --run <N>

# 提交本轮
sop-board run submit --run <N> [--implementer <实现者会话编号> | --implemented-by <实现者的操作者>]   # 实现者按 prompt.md 的说明填，没说明就都不填

# 查看
npx --no-install playwright show-report playwright/html
```

- `convert` 把测试标题第一个词当场景 key，逐项核对报告里的 `sop-scope` 注解与 scope.json。`--out .` 使证据路径成为 `evidence/…` 相对路径。
- 中途退出时用 `playwright/report.partial.json` 加 `--allow-partial`；按运行目录 prompt.md 核对已执行范围。仅因时间不足而未做的场景不写结果行。
- 在 report 搜索框输入 `@shop#W` 可筛选该场景；标签只标识场景，通过与否以 `run verdict` 与 `run submit` 的登记为准。
- 转换后的结果经 `run verdict` 裁决，`run submit` 完成本轮。
- `sop-board playwright` 的子命令只有 `guide`、`convert`。
