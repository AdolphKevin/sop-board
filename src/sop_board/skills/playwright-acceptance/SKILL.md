---
name: playwright-acceptance
description: 用 Playwright 连接用户已打开的 Chrome，按场景文件中的明确步骤执行 Web 场景验收（含 Shopify 内嵌应用），录屏、截图与 trace 进入 Playwright HTML report，结果交回 sop-board 登记并沉淀为回归检查。适用于：按 SOP 场景做浏览器黑盒验收、把已通过的场景固化成 Playwright 回归、在 report 中按上游用例编号查看结果。
disable-model-invocation: true
---

# Playwright 场景验收

本技能把上游写好的场景步骤执行成 Playwright 测试。它不生成测试用例，不判断业务含义，也不自建看板；看板就是 Playwright 自带的 HTML report 与 Trace Viewer。
场景与业务预期由上游提供，结果采用 sop-board 的 JSON Lines 契约。本技能负责从已确认步骤到浏览器执行和证据转换；随 sop-board 发行，由当前看板任务登记到原工作区和批次。

## 适用边界

- 浏览器：复用用户已打开的 Chrome（CDP），不新开浏览器。建议用测试专用的 Chrome profile，详见[通用 Web 参考](references/web.md#浏览器与连接)。
- 账号与数据：只在测试店铺、测试环境操作。真实账号的登录、人机验证、二次验证都由人完成，代理遇到即停止。
- 独立性：首次验收的执行者不能是实现该功能的人或代理。实现者自己跑脚本只算自检，不能作为验收证据。
- 证据：结果与证据放在业务仓库之外的本轮任务目录，放进仓库会改变产品指纹，使已通过场景失效。

## 工作流程

1. **读输入。** 场景文件（`docs/sop/*.yaml`）、看板任务的 `pending.json` 与 Web 专用的 `web-pending.json`、项目的界面地图（`surface-map.md`）。先固定原工作区、批次、场景编号和指纹，使用[执行范围快照](references/run-scope.md)防止串批次或漏测。只有业务目标、前置分支或预期不明确时退回对应场景；缺少按钮名称、iframe 或定位器可在下一步只读探测补全，不改变业务含义。
2. **弄清界面结构并复用状态。** 有产品源码时先读源码（路由、多语言文案、打开弹窗的调用），再在真实页面做只读探测并补充操作映射；没有源码时只做只读探测，做法见[只读探测](references/web.md#只读探测)。目标是确认每一步的目标在哪一层：页面本身、应用 iframe、App Bridge modal、新标签页。先用当前可见内容确认目标是否已就绪；满足导航前提时直接复用当前视图及所在容器，只有不满足时才走项目映射中的必要导航。场景明确要求重新打开、刷新或重置时仍按原步骤执行。结论写进界面地图；没有源码核对的条目在"依据"列写"未核对源码，探测于 <日期>"，产品改版后要重新探测。
3. **写脚本。** 把 `templates/playwright.config.mjs` 与 `global-setup.mjs` 复制到仓库外的本轮任务目录，`fixtures.mjs`、`connection.mjs`、`recording.mjs`、`scope.mjs`、浏览器执行脚本与 `surface-map.md` 放进其下的 `scenarios/`。`global-setup.mjs` 在开跑前用当前项目的 Playwright 检查版本、ffmpeg、scope 和调试端口；共享 worker 首次连接时核对 CDP、唯一目标页及可选 `PW_FRAME`，失败不执行业务步骤。预检不额外建立 CDP 会话，worker 内各用例共用一次连接、分别录屏和保存 trace。交互式 Chrome 使用模板的 `maxFailures: 1`；`workers: 1`、`retries: 0` 本身不能阻止失败后 worker 重启和重新授权。首个意外失败或受阻后停止本轮；连接异常时不让后续用例继续重连。多休眠标签页可选 `PW_CDP_MODE=target`（Node >=22）；不把超时直接解释为用户未同意。一条步骤对应一个 `step()`，标题照抄场景步骤；用例标题以场景编号开头；tag 由 `scripts/case_tags.py` 从配对文件生成，不手写。定位优先用角色和可见文字，其次稳定属性；没有可用属性时建议产品方补 `data-testid`。
4. **核对前置条件。** 开跑前先核对已知的账号、对象和数据可用性，避免明知缺失仍连接浏览器。执行前按 `initial_state` 逐项核对；复用视图不等于复用上一条的数据或授权状态。不满足时抛出以 `受阻：` 开头的错误并停止本轮，不把剩余未执行场景批量写成受阻，不自己造数据。
5. **执行。** `npx --no-install playwright test`，保留 `workers: 1`、`retries: 0`、`maxFailures: 1`。定位失败先停止当前动作并只读核对当前页面、容器和定位依据，不循环点击旧入口。发送、保存、接管、支付等动作只提交一次；预先注册需要等待的请求或页面事件，按项目映射等待完成信号并核对可见结果，再刷新、导航或结束页面。失败或完成状态不明时不自动重试，由人决定恢复数据还是判失败。
6. **核对结果。** 只断言用户看得见的结果，不用 URL 或内部状态代替；逐项核对 `forbidden_effects`。预期不符即失败；人机验证、登录过期、未知遮挡、前置条件不满足为受阻。
7. **提交。** `sop-board playwright convert` 核对同一 scope、场景原文版本和报告注解，转换结果并复制证据。提供 pending/scope 时默认要求范围内结果完整；部分试跑显式加 `--allow-partial` 并呈报 `coverage.json` 中的未执行编号。提前停止时保留原始报告，核对实际执行编号，用 `--allow-partial` 列明未执行范围；不把整个批次转换成跳过或受阻。提交回输入的原工作区与批次，随后读取看板核对编号、结果和已执行数；不自行新建批次或把子步骤通过等同整条场景通过。未提供 scope 时不宣称已校验批次和内容指纹。回归沉淀见下文。

## 命令

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

## 受阻与失败

| 情况 | 处理 | 契约状态 |
| --- | --- | --- |
| 预期不符、禁止的副作用发生 | 让断言失败 | `fail` |
| 连接/录屏准备失败、前置条件不满足、人机验证、登录过期、来源不明的遮挡 | `throw new Error('受阻：<原因>')` | `blocked`，需问题编号 |
| 场景本轮不执行 | `test.skip()` 并说明 | `skip`，需问题编号 |

先确认操作确实作用于正确的可见对象与容器。定位依据失效、错误容器或连接故障记为带原因的执行受阻，不冒充产品缺陷；正确操作后的业务结果不符仍为失败，不能把所有超时都改成受阻。清理和附件登记失败必须留记录，且不能覆盖原业务错误。

不要为了跑通把受阻改成跳过或通过；也不要自动关闭来源不明的遮挡层，它可能正是要发现的缺陷。

## 回归沉淀

首次验收后的浏览器执行脚本和界面地图保留在看板任务目录，后续按场景重新执行。入口由工作区 config.yaml 声明，不新增 checks 或场景 regression 字段，不恢复已删除的测试套件。依赖已登录 Chrome 的脚本只在本机运行，如实报告实际执行与独立性。

## 参考

- [通用 Web 参考](references/web.md)：连接、定位、iframe 与弹窗、等待、证据。
- [Shopify 参考](references/shopify.md)：`app-iframe`、App Bridge modal、主题编辑器、人机验证、登录。
- [结果与配对契约](references/contracts.md)：sop-board 的结果字段、来源与提交方式。
- [执行范围快照](references/run-scope.md)：原批次绑定、完整/部分执行与结果核对。
- [场景写法要求](references/scenario-steps.md)：对上游场景的要求与返工清单。
- `templates/`：夹具、配置、示例 spec、界面地图模板。

看板自动任务的 scope 只列本轮 Web 场景；混合入口最终 results.jsonl 还须合并其他驱动的结果，覆盖总 pending。修复复测按 playwright-scopes.json 分批转换，保留原 batch，避免同编号跨批次混用。

当前工作区版本使用 config.yaml 声明入口，sop-board guide 是工作区搭建指引；playwright guide 为 Web 驱动指引。手工 scope 用 --workspace，既有 project 字段存工作区键，提交走 /api/v1/workspaces/。工作区的 source-changed 沿用和跨仓修复范围以任务提示为准，修复复测不得沿用。
