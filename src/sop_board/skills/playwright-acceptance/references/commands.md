# 命令

在运行目录执行，先 `set -a; . ./run.env; set +a`。

```bash
# 上游配对文件 → 场景→用例 tag 映射（spec 通过 PW_CASES 读取）
sop-board playwright tags --pairs <配对.json> --out <运行目录外>/cases.json

# 执行（环境变量见 templates/fixtures.mjs 顶部注释；PW_OUT、PW_SCOPE 等已在 run.env）
PW_CASES=<目录>/cases.json npx --no-install playwright test

# 转成结果契约；blocked 与 skip 用 --finding 写原因；超过 64 MiB 的证据加 --skip-large
sop-board playwright convert --report playwright/report.json --out . --executor <会话> \
  --scope scope.json --scenario-file scenarios.yaml [--finding shop#W=<原因>] [--allow-partial] [--skip-large]

# 登记；其他入口的结果须已合并进 results.jsonl
sop-board run submit --run <N> [--implementer <实现者会话编号>]

# 查看
npx --no-install playwright show-report playwright/html
```

- `convert` 把测试标题第一个词当场景 key，逐项核对报告里的 `sop-scope` 注解与 scope.json。`--out .` 使证据路径成为 `evidence/…` 相对路径。
- 中途退出时用 `playwright/report.partial.json` 加 `--allow-partial`；未执行的场景写成 `not-executed` 行，登记时留在待执行范围。
- 在 report 搜索框输入 `@CASE-037` 可筛出覆盖该上游用例的测试。tag 只表示覆盖关系，通过与否以 `run submit` 登记的结果为准。
- 只用 `convert` 与 `run submit` 登记结果，不另行提交第二份。
- `sop-board playwright` 的子命令只有 `guide`、`convert`、`tags`。
