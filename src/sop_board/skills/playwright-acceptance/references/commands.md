# 命令

```bash
# 从 sop-board 配对文件生成 场景→用例 tag 映射（spec 通过 PW_CASES 读取）
sop-board playwright tags --pairs <配对.json> --out <仓库外目录>/cases.json

# 执行（环境变量见 templates/fixtures.mjs 顶部注释）
PW_TAB='/apps/<应用>/' PW_CASES=<目录>/cases.json PW_OUT=<仓库外目录> \
  PW_SCOPE=<目录>/scope.json PW_SCENARIO_FILE=<场景原文> npx --no-install playwright test

# 转成看板结果契约；受阻与跳过的场景要给出问题编号；超过 64 MiB 的证据加 --skip-large 跳过
sop-board playwright convert --report <目录>/report.json --out <目录>/round-1 \
  --executor "<执行者>" --finding SOP-02-P01=Q3 --pending <目录>/pending.json --scope <目录>/scope.json --scenario-file <场景原文> [--session <轮次>]
# 自动任务：--out 用任务目录；写完 results.jsonl 后交回看板，不额外提交轮次。
# 手工试跑才提交（SOP_BOARD_URL 由环境提供；当前工作区 API 无需令牌）：
sop-board playwright submit --results <目录>/round-1/results.jsonl --scope <目录>/scope.json \
  --scenario-file <场景原文> --product <产品快照.json> --idempotency-key <本轮唯一编号> --submitted-by <执行者>

# 查看
npx --no-install playwright show-report <目录>/html
```

在 report 搜索框输入 `@CASE-037` 可筛出覆盖该上游用例的全部测试，再加 `s:failed` 只看失败。tag 只表示"覆盖哪条用例"；一条用例是否通过，以 sop-board 按配对与轮次算出的结果为准。

