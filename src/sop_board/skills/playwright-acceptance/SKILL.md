---
name: playwright-acceptance
description: 连接已打开的测试 Chrome，按已确认场景执行 Web 验收、复用和审核规范脚本、按安全声明巡检定位，并把业务结果交回 sop-board。产品回归测试建设使用 regression-distill。
disable-model-invocation: true
---

# Playwright 场景验收

按已确认场景执行，依入口留证并转换为看板结果，登记到原批次；业务预期来自上游。

## 适用边界

- 浏览器：复用用户已打开的 Chrome（CDP），不新开浏览器；建议用测试专用 profile，见[通用 Web 参考](references/web.md#浏览器与连接)。
- 账号与数据：只在测试环境操作；真实账号登录、人机验证、二次验证由人完成，代理遇到即停止。
- 独立性：执行者不能是实现者，实现者跑脚本只算自检。
- 证据：按场景 `verify` 所引用入口的 `evidence` 逐项留证。录屏与 trace 的开关已按场景固定在 `scope.json`（`PW_SCOPE`），不自行覆盖。截图与只读查询用 `testInfo.attach` 附到报告，放在仓库外的任务目录。

## 工作流程

操作分支见[执行细则](references/workflow.md)。

1. **读输入。** 先读任务目录的 `brief.md`，再读场景原文、`entries.json`、`pending.json`、`web-pending.json`，用[执行范围快照](references/run-scope.md)固定原批次、场景编号与指纹。有交互地图时以 `atlas.json`、`bindings.json` 为准；没有地图时才用项目的 `surface-map.md`。
2. **弄清界面。** 地图给出页面、控件、各语言文案与定位器；地图缺失或有误时，只读探测或查阅前端源码，并把缺口写进 `atlas-feedback.jsonl`。先复用当前视图，只在前提不满足时导航。
3. **先复用，再补齐。** 先读 specs-manifest.json，seeded 使用任务冻结副本，missing 才新写，reference 对照当前要求后改写，conflict 保留原文件并处理。不从共享目录加载，不覆盖已有候选。缺少运行器时把 `playwright.config.mjs`、`global-setup.mjs`、`progress-reporter.mjs`、`spec-assets.mjs` 按缺失项复制到任务目录，`fixtures.mjs`、`connection.mjs`、`recording.mjs`、`scope.mjs`、`atlas.mjs` 放进 `scenarios/`。一条步骤对应一个 `step()`，用例标题以场景编号开头，tag 由 `scripts/case_tags.py` 生成。
4. **准备前置。** 按 `initial_state` 逐项核对。无关前置数据按 `fixtures.json` 用 `sop-board fixtures run` 准备，没有合适配方且允许时用 `fixtures exec`；`real_only` 与被验收流程应产生的对象必须经真实入口产生。仍无法准备时，`throw blocked('<缺什么>')`。
5. **执行。** `npx --no-install playwright test`，保留 `workers: 1`、`retries: 0`、`maxFailures: 1`，时间上限见 `brief.md` 或执行细则。保存、发送、支付等动作只提交一次，等完成信号后再核对。
6. **核对结果。** 只断言用户看得见的结果，逐项核对 `forbidden_effects`。界面对照中“按 SOP”与“未定”的条目，实际文案不符即判失败。
7. **转换与提交。** 用 `sop-board playwright convert` 转换报告；执行中途退出时，用 `report.partial.json` 加 `--allow-partial` 转换已完成的场景。命令见[命令](references/commands.md)。

## 改写、巡检与收入

修改已有脚本、准备收入或改 UI 后巡检时，先读[规范库与巡检](references/spec-library.md)：check 输出标识，spec-ledger 逐项记账，撤销绑定真实业务决定；实际运行并登记后，由未参与改写者 review/promote，收入推进基线。patrol 须审核路径、请求用途、数量与未覆盖范围；未知请求发出前停止、越界停止，失败留截图，不登记场景。库可受版本管理地进入产品仓库，任务副本与证据仍在仓库外；辅助入口默认 .off，启用须写 note。

## 失败与受阻

业务断言不符记 fail，留截图与页面结构，本轮继续；连接、前置、人机验证、登录过期、未知遮挡或目标页关闭记 blocked，停止本轮并单列未执行范围；主动 test.skip 须说明理由。blocked/skip 须问题编号。不自动关闭未知遮挡，不为跑通更改结论。

只重跑未通过项，业务失败最多再确认一次；单场景累计 15 分钟或 4 次即止损，完整要求见[执行细则](references/workflow.md)。

## 参考

| 文件 | 内容 |
| --- | --- |
| [执行细则](references/workflow.md) | 完整步骤与停止规则 |
| [命令](references/commands.md) | 执行、转换与提交 |
| [通用 Web 参考](references/web.md) | 连接、定位与证据 |
| [Shopify 参考](references/shopify.md) | 内嵌应用与验证停止 |
| [结果与配对契约](references/contracts.md) | 字段与来源 |
| [执行范围快照](references/run-scope.md) | 范围与原批次 |
| [场景写法要求](references/scenario-steps.md) | 对上游场景的要求 |
| `templates/` | 配置、夹具、地图操作库、进度报告器、示例 spec、界面地图模板 |
