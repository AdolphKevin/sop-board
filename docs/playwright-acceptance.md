# 内置 Web 验收

技能、模板、转换器及既有资源统一维护于 `src/sop_board/skills/playwright-acceptance/`，随包发行，不依赖 team-workflow 或单独的技能检出目录。

## 安装与入口

```sh
uv tool install /path/to/sop-board
sop-board guide
sop-board playwright guide
```

`guide` 保留 main 的工作区搭建指引并列出 Web 入口，不启动服务。`playwright guide` 只显示 Web 操作。默认使用包内技能；显式 SOP_BOARD_SKILLS_DIR 下存在 playwright-acceptance/SKILL.md 时可覆盖。自检使用同一解析规则，其余驱动遵守工作区配置。

## 自动执行与修复复测

执行任务提供 scenarios.yaml、总 pending.json、Web 专用 web-pending.json、scope.json、entries.json 及快照。保持工作区已启动的适配环境，按入口操作，不自行启停服务。Web 连接在同一 worker 共用 CDP，每条实际执行场景独立录屏、截图及 trace。

```sh
sop-board playwright convert --report <报告.json> --out <任务目录> \
  --scope <任务目录>/scope.json --scenario-file <任务目录>/scenarios.yaml \
  --pending <任务目录>/web-pending.json --executor <执行会话>
```

混合入口合并各驱动结果到同一 results.jsonl，覆盖总 pending。仅 source-changed 场景可按主流程规则沿用；Web 报告只包含实际执行项时，使用实际执行编号的 pending、显式 --allow-partial，并将沿用结果与报告结果合并后交回。不得漏掉未沿用场景。自动任务由看板登记，不调用手工 submit。

修复复测通过 playwright-scopes.json 按批次获取独立场景原文和 scope，分别转换后补 batch，证据路径统一相对任务目录 evidence/。修复复测不沿用原通过结果。

## 手工登记

```sh
sop-board playwright scope --workspace <工作区键> --scenario-file <原文.yaml> --out /work/scope.json
sop-board playwright convert --report /work/report.json --out /work/round \
  --scope /work/scope.json --scenario-file <原文.yaml> --executor <执行者>
sop-board playwright submit --results /work/round/results.jsonl --scope /work/scope.json \
  --scenario-file <原文.yaml> --product /work/product.json \
  --idempotency-key <本轮唯一编号> --submitted-by <执行者> --url http://127.0.0.1:8787
```

工作区键使用页面与 API 中的实际名称，不把仓库身份转换为旧项目键。scope 的既有 project 字段保存该工作区键，--project 仅为兼容参数别名。看板须先有同一场景原文；submit 使用 `/api/v1/workspaces/<键>/batches/<批次>/rounds`，无需令牌，不跟随重定向，上传证据前核对原文版本。地址也可通过 SOP_BOARD_URL 提供。product.json 包含 commit、digest（兼容 product_digest/product_fingerprint）、dirty；多仓版本取当前工作区快照，不把 HEAD 当成运行态证明。

完整执行默认拒绝漏测；部分试跑显式 --allow-partial，并检查 coverage.json。保留旧数据目录：main 的 v9 版本不读取或迁移旧看板数据，不把手工登记当成迁移工具。

## 验证与维护

遵循 main 的场景验收方式，通过实际命令、工作区 HTTP 入口与页面核对，不恢复上游删除的根 tests/。现有技能资源随包保留；发行包应包含技能、模板和转换器。提交后核对工作区、批次、编号、轮次、证据及进度，不以 Playwright 的绿色数量代替整批业务结果。
