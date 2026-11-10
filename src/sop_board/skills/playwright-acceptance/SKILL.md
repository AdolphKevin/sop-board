---
name: playwright-acceptance
description: 在 sop-board 运行目录中，经看板守护进程启动的 Chrome for Testing 执行 Web 场景验收，转换报告并交回 run submit。产品回归测试建设使用 regression-distill。
disable-model-invocation: true
---

# Playwright 场景验收

按 `sop-board run prepare` 准备好的运行目录执行已确认场景，依入口留证，转换为 results.jsonl 后由 `sop-board run submit` 登记。业务预期来自 scenarios.yaml。

## 边界

- 浏览器：只连接 run.env 的 `PW_CDP_ENDPOINT`，即看板守护进程为本检出×身份启动的 Chrome for Testing（默认无头，持久 profile）。执行者不启动浏览器、不运行 `playwright install`、不接触用户日常 Chrome。
- 账号：只在测试环境操作。真实账号由人用 `sop-board browser open --identity <身份> --headed` 登录一次，随后 `sop-board browser close`；之后的无头运行沿用 profile。登录、人机验证、二次验证由人完成，出现 "browser may not be secure" 即停止并记 blocked。
- 独立性：执行者不能是实现者，实现者跑脚本只算自检。
- 证据：按场景 `verify` 所引用入口的 `evidence` 逐项留证，截图与只读查询用 `testInfo.attach` 附到报告。录屏与 trace 开关已按场景固定在 `scope.json`，不自行覆盖。

## 流程

1. **读输入。** 读运行目录的 `brief.md` 与 `prompt.md`，再读 `scenarios.yaml`（场景 `key` 为 `<文件>#<编号>`）、`pending.json`、`entries.json`、`identities.json`。`scope.json` 固定本检出、场景原文摘要与各场景指纹（见[执行范围](references/run-scope.md)）。`set -a; . ./run.env; set +a`。
2. **弄清界面。** 运行目录有 `atlas.json`（PW_ATLAS）时先用它定位页面与控件，`stale` 中的页面以实际页面为准。其余情况有产品源码时读路由与文案，再对真实页面只读探测；无源码时只探测，做法见[Web 参考](references/web.md#只读探测)。结论写入 `surface-map.md`（模板在 `templates/`）。已就绪的视图直接复用，只在前提不满足时导航。
3. **补齐运行器与脚本。** 把 `playwright.config.mjs`、`global-setup.mjs`、`progress-reporter.mjs` 复制到运行目录根部，`fixtures.mjs`、`connection.mjs`、`recording.mjs`、`scope.mjs`、`identity.mjs`（有地图时另加 `atlas.mjs`）放进 `scenarios/`。依赖由 `node_modules` 符号链接指向看板运行时。一条场景步骤对应一个 `step()`，用例标题以场景 key 开头（`shop#W 标题`）；上游用例 tag 用 `scripts/case_tags.py` 生成。
4. **准备前置。** 按 `initial_state` 逐项核对。无关前置数据按 brief.md 的前置数据一节准备；被验收流程应产生的对象经真实入口产生。准备不了时 `throw blocked('<缺什么>')`。真实登录或请求验证码前先登记使用次数（见[Web 参考](references/web.md#多身份与使用次数)）。
5. **执行。** `npx --no-install playwright test`，保留 `workers: 1`、`retries: 0`、`maxFailures: 1`。保存、发送、支付等动作只提交一次，等完成信号后再核对。
6. **核对结果。** 只断言用户看得见的结果，逐项核对 `forbidden_effects`。
7. **转换与提交。** 在运行目录：

   ```bash
   sop-board playwright convert --report playwright/report.json --out . --executor <会话> \
     --scope scope.json --scenario-file scenarios.yaml [--finding <key>=<原因>] [--allow-partial]
   sop-board run submit --run <N>
   ```

   证据复制到 `evidence/<key，# 换成 __>/`。其他入口的结果行合并进同一 results.jsonl。命令细节见[命令](references/commands.md)。

## 结果判定

业务断言不符记 fail，留截图与页面结构，本轮继续。连接、前置、人机验证、登录过期、未知遮挡、目标页关闭、浏览器或渲染进程崩溃记 blocked，停止本轮并把剩余场景记 not-executed。判定表、崩溃处理与止损规则见[执行细则](references/workflow.md)。

## 参考

| 文件 | 内容 |
| --- | --- |
| [执行细则](references/workflow.md) | 失败与受阻判定、崩溃、重试与止损 |
| [命令](references/commands.md) | 运行、转换、提交与标签 |
| [Web 参考](references/web.md) | 连接、身份、定位、前置数据与证据 |
| [Shopify 参考](references/shopify.md) | 内嵌应用与验证停止 |
| [结果与配对契约](references/contracts.md) | 场景与结果字段、配对标签 |
| [执行范围](references/run-scope.md) | scope.json、完整与部分执行 |
| [场景写法要求](references/scenario-steps.md) | 对上游场景的要求与退回清单 |
| [浏览器层 spec 保真](references/spec-fidelity.md) | 改写或撤销已有 spec 断言时的检查与账本 |
| `templates/` | 运行器配置、夹具、连接与身份、示例 spec、界面地图模板 |
