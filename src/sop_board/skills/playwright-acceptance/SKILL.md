---
name: playwright-acceptance
description: 用 Playwright 连接用户已打开的 Chrome，按已确认的 SOP 步骤执行 Web 场景验收（含 Shopify 内嵌应用），按工作区入口声明保存证据，并将结果交回 sop-board。适用于浏览器黑盒验收和按上游用例编号查看报告；把场景沉淀为产品回归测试时使用 regression-distill。
disable-model-invocation: true
---

# Playwright 场景验收

本技能把上游已确认的场景步骤执行成 Playwright 测试，按工作区入口声明留证，并把结果转换为 sop-board 的 JSON Lines 契约，登记到原工作区和批次。它不生成用例，不判断业务含义。

## 适用边界

- 浏览器：复用用户已打开的 Chrome（CDP），不新开浏览器；建议用测试专用 profile，见[通用 Web 参考](references/web.md#浏览器与连接)。
- 账号与数据：只在测试环境操作；真实账号登录、人机验证、二次验证由人完成，代理遇到即停止。
- 独立性：执行者不能是实现者，实现者跑脚本只算自检。
- 证据：按场景 `verify` 所引用入口的 `evidence` 逐项留证。录屏与 trace 的开关已按场景固定在 `scope.json`（`PW_SCOPE`），不自行覆盖。截图与只读查询用 `testInfo.attach` 附到报告，放在仓库外的任务目录。

## 工作流程

每步的完整要求见[执行细则](references/workflow.md)，遇到对应情况时再读。

1. **读输入。** 先读任务目录的 `brief.md`，再读场景原文、`entries.json`、`pending.json`、`web-pending.json`，用[执行范围快照](references/run-scope.md)固定原批次、场景编号与指纹。有交互地图时以 `atlas.json`、`bindings.json` 为准；没有地图时才用项目的 `surface-map.md`。
2. **弄清界面。** 地图给出页面、控件、各语言文案与定位器；地图缺失或有误时，只读探测或查阅前端源码，并把缺口写进 `atlas-feedback.jsonl`。先复用当前视图，只在前提不满足时导航。
3. **写脚本。** 把 `playwright.config.mjs`、`global-setup.mjs`、`progress-reporter.mjs` 复制到任务目录，`fixtures.mjs`、`connection.mjs`、`recording.mjs`、`scope.mjs`、`atlas.mjs` 放进 `scenarios/`。一条步骤对应一个 `step()`，用例标题以场景编号开头，tag 由 `scripts/case_tags.py` 生成。
4. **准备前置。** 按 `initial_state` 逐项核对。无关前置数据按 `fixtures.json` 用 `sop-board fixtures run` 准备，没有合适配方且允许时用 `fixtures exec`；`real_only` 与被验收流程应产生的对象必须经真实入口产生。仍无法准备时，`throw blocked('<缺什么>')`。
5. **执行。** `npx --no-install playwright test`，保留 `workers: 1`、`retries: 0`、`maxFailures: 1`，时间上限见 `brief.md` 或执行细则。保存、发送、支付等动作只提交一次，等完成信号后再核对。
6. **核对结果。** 只断言用户看得见的结果，逐项核对 `forbidden_effects`。界面对照中“按 SOP”与“未定”的条目，实际文案不符即判失败。
7. **转换与提交。** 用 `sop-board playwright convert` 转换报告；执行中途退出时，用 `report.partial.json` 加 `--allow-partial` 转换已完成的场景。命令见[命令](references/commands.md)。

## 失败与受阻

| 情况 | 处理 | 契约状态 |
| --- | --- | --- |
| 预期不符、禁止的副作用发生 | 让断言失败；模板把它标为预期失败，留截图与页面结构快照，本轮继续执行后续场景 | `fail` |
| 连接或录屏准备失败、前置条件不满足、人机验证、登录过期、来源不明的遮挡、目标页关闭 | `throw blocked('<原因>')`，或由连接断开触发；本轮停止，剩余场景列为未执行 | `blocked`，需问题编号 |
| 场景本轮不执行 | `test.skip()` 并说明 | `skip`，需问题编号 |

**重试与止损**：只重跑没通过的场景；单个场景累计 15 分钟或 4 次仍无结论就转下一个（细则见执行细则）。不为跑通把受阻改成跳过或通过，不自动关闭来源不明的遮挡。

## 参考

| 文件 | 内容 |
| --- | --- |
| [执行细则](references/workflow.md) | 各步骤的完整要求、受阻判定、回归沉淀、任务与工作区约定 |
| [命令](references/commands.md) | tags、执行、转换、手工提交、查看报告 |
| [通用 Web 参考](references/web.md) | 连接、定位、iframe 与弹窗、等待、证据、前置数据 |
| [Shopify 参考](references/shopify.md) | 内嵌应用、App Bridge、主题编辑器、人机验证 |
| [结果与配对契约](references/contracts.md) | 结果字段、来源与提交方式 |
| [执行范围快照](references/run-scope.md) | 原批次绑定、完整与部分执行 |
| [场景写法要求](references/scenario-steps.md) | 对上游场景的要求 |
| `templates/` | 配置、夹具、地图操作库、进度报告器、示例 spec、界面地图模板 |
