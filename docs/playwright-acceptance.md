# Web 验收由 sop-board 维护

规范、模板、转换器与测试统一位于 `src/sop_board/skills/playwright-acceptance/`，随 Python 包发行。它包含证据附件隔离、范围与指纹校验、目标页连接、worker 共用 CDP、逐用例录屏/trace、初始化失败清理和业务步骤到元素定位的规则。team-workflow 不再是开发或验收前置依赖；旧字段与历史资料保持读取兼容。

## 安装与查找

```sh
uv tool install /path/to/sop-board
sop-board guide
sop-board playwright guide
```

重新安装已有本机工具时可加 `--reinstall`。guide 不启动 HTTP 服务，不需要令牌。默认 Web 驱动来自包内；`SOP_BOARD_SKILLS_DIR/playwright-acceptance/SKILL.md` 存在时才显式覆盖 Web 驱动。其他驱动仍可从该环境变量或 `~/.agents/skills` 读取。

## 看板自动执行

看板将筛选后的 scenarios.yaml、总 pending.json、Web 专用 web-pending.json 和 scope.json 写入任务目录，提示词明确给出实际内置技能路径。scope 绑定原项目、批次、当前筛选场景原文摘要与 Web 场景内容指纹。

执行者读取技能，在任务目录安装/使用 Playwright，`PW_SCOPE` 指向 scope.json，`PW_SCENARIO_FILE` 指向 scenarios.yaml，证据输出放在仓库外的该任务目录。同一个 worker 建立一次 CDP 连接，每条用例单独录制。全局预检只检查依赖、scope 与 TCP，worker 首次连接检查目标页和 iframe；失败重启 worker 或手动重跑可能重新要求 Chrome 授权。

```sh
sop-board playwright convert --report <任务目录>/playwright/report.json \
  --out <任务目录> --scope <任务目录>/scope.json \
  --scenario-file <任务目录>/scenarios.yaml --pending <任务目录>/web-pending.json \
  --executor <执行会话编号>
```

混合入口的非 Web 结果须合并进同一个 results.jsonl，最终覆盖总 pending。看板自己补指纹与身份、校验证据并登记，代理不再运行任何旧流程命令，也不调用手工 submit。

修复复测按 playwright-scopes.json 为每个批次准备独立场景与范围快照；每批转换后补回 batch 并汇总，证据路径均相对复测任务目录的 evidence/，避免同编号跨批次混用。

## 手工试跑

```sh
sop-board playwright scope --project github.com/example/project \
  --scenario-file /repo/docs/sop/batch.yaml --out /work/scope.json
sop-board playwright convert --report /work/playwright/report.json --out /work/round \
  --scope /work/scope.json --scenario-file /repo/docs/sop/batch.yaml --executor <执行者>
sop-board playwright submit --results /work/round/results.jsonl --scope /work/scope.json \
  --scenario-file /repo/docs/sop/batch.yaml --product /work/product.json \
  --idempotency-key <本轮唯一编号> --submitted-by <执行者>
```

看板须先存在同一场景原文。SOP_BOARD_URL、SOP_BOARD_TOKEN 只经环境变量提供；submit 不接受命令行令牌，不跟随重定向，先核对看板原文再上传证据。product.json 包含 commit、digest（也接受 product_digest/product_fingerprint）及 dirty；调用方核对其与实际被测版本的关系。完整执行拒绝漏测；部分试跑须显式 --allow-partial 并报告未测范围。

## 验证

Python 与 Node 单元测试位于内置技能 tests/。`test_worker_reuse.mjs` 需 Playwright >=1.64 和 ffmpeg，PW_TEST_PACKAGE 可指定包目录；它通过真实 Playwright 调度验证四条用例只连接、断开一次，同时保存四套录屏/trace，不操作真实 Chrome。看板根测试验证 CLI、原批次 API 登记、快照生成及过期版本拒绝。发行包须包含整个内置技能资源目录。
