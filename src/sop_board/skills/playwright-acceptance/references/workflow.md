# Web 验收执行细则

本文件是 [SKILL.md](../SKILL.md) 各步骤的完整要求，按需阅读；与正文冲突时以本文件为准。

## 工作流程细则

1. **读输入。** 场景文件（`docs/sop/*.yaml`）、看板任务的 `entries.json`、`pending.json` 与 Web 专用的 `web-pending.json`。工作区为该入口梳理过交互地图时，以地图为准，执行中只把缺口写进 `atlas-feedback.jsonl`，不再每轮新建界面地图；没有地图时才用项目的界面地图（`surface-map.md`）。先固定原工作区、批次、场景编号和指纹，使用[执行范围快照](references/run-scope.md)防止串批次或漏测。只有业务目标、前置分支或预期不明确时退回对应场景；缺少按钮名称、iframe 或定位器可在下一步只读探测补全，不改变业务含义。
2. **弄清界面结构并复用状态。** 有产品源码时先读源码（路由、多语言文案、打开弹窗的调用），再在真实页面做只读探测并补充操作映射；没有源码时只做只读探测，做法见[只读探测](references/web.md#只读探测)。目标是确认每一步的目标在哪一层：页面本身、应用 iframe、App Bridge modal、新标签页。先用当前可见内容确认目标是否已就绪；满足导航前提时直接复用当前视图及所在容器，只有不满足时才走项目映射中的必要导航。场景明确要求重新打开、刷新或重置时仍按原步骤执行。结论写进界面地图；没有源码核对的条目在"依据"列写"未核对源码，探测于 <日期>"，产品改版后要重新探测。
3. **写脚本。** 把 `templates/playwright.config.mjs` 与 `global-setup.mjs` 复制到仓库外的本轮任务目录，`progress-reporter.mjs` 也放在任务目录根部；`fixtures.mjs`、`connection.mjs`、`recording.mjs`、`scope.mjs`、`atlas.mjs`、浏览器执行脚本放进其下的 `scenarios/`；没有交互地图时另放 `surface-map.md`。`global-setup.mjs` 在开跑前用当前项目的 Playwright 检查版本、scope 和调试端口，仅本轮需要录屏时检查 ffmpeg；共享 worker 首次连接时核对 CDP、唯一目标页及可选 `PW_FRAME`，失败不执行业务步骤。预检不额外建立 CDP 会话，worker 内各用例共用一次连接，按各自入口开关录屏和保存 trace。模板把业务失败标为预期失败（D87）：只记该场景失败并留截图与页面结构快照，关闭本场景新开的页面，worker 不重启、连接保留，后续场景照常执行；受阻、连接断开、目标页关闭仍是意外失败，`maxFailures: 1` 在首个出现时停止本轮，连接异常时不让后续用例继续重连。`workers: 1`、`retries: 0` 本身不能阻止意外失败后 worker 重启和重新授权。多休眠标签页可选 `PW_CDP_MODE=target`（Node >=22）；不把超时直接解释为用户未同意。一条步骤对应一个 `step()`，标题照抄场景步骤；用例标题以场景编号开头；tag 由 `scripts/case_tags.py` 从配对文件生成，不手写。定位优先用角色和可见文字，其次稳定属性；没有可用属性时建议产品方补 `data-testid`。
4. **核对前置条件。** 开跑前先核对已知的账号、对象和数据可用性，避免明知缺失仍连接浏览器。执行前按 `initial_state` 逐项核对；复用视图不等于复用上一条的数据或授权状态。无关前置数据先按本轮 `fixtures.json` 用 `sop-board fixtures run` 准备；没有合适配方且 `adhoc: true` 时，可在声明环境与范围内用 `fixtures exec --reason` 临时写入。两者带 `--task`、`--scenario` 留痕。`real_only` 对象与本场景流程应产生的对象必须经真实入口产生，不能写库代替。仍无法准备时抛出以 `受阻：` 开头的错误并停止本轮，finding 写缺什么配方、范围或真实流程；剩余未执行场景不批量写成受阻。完整边界见[前置数据](references/web.md#前置数据)。
5. **执行。** `npx --no-install playwright test`，保留 `workers: 1`、`retries: 0`、`maxFailures: 1`。缺省时间上限为单场景 4 分钟、定位或操作 15 秒、导航 30 秒（`PW_SCENARIO_TIMEOUT_MS`、`PW_ACTION_TIMEOUT_MS`、`PW_NAVIGATION_TIMEOUT_MS` 可覆盖，记入报告 metadata.limits）。有地图时设置 `PW_ATLAS`，用 `atlas.mjs` 的 `openPage`、`locate`、`act` 按地图操作，`AtlasGap` 表示地图缺口，写进任务目录 `atlas-feedback.jsonl` 后再只读探测。定位失败先停止当前动作并只读核对当前页面、容器和定位依据，不循环点击旧入口。发送、保存、接管、支付等动作只提交一次；预先注册需要等待的请求或页面事件，按项目映射等待完成信号并核对可见结果，再刷新、导航或结束页面。失败或完成状态不明时不自动重试，由人决定恢复数据还是判失败。
6. **核对结果。** 只断言用户看得见的结果，不用 URL 或内部状态代替；逐项核对 `forbidden_effects`。预期不符即失败；人机验证、登录过期、未知遮挡、前置条件不满足为受阻。
7. **提交。** `sop-board playwright convert` 核对同一 scope、场景原文版本和报告注解，转换结果并复制证据。提供 pending/scope 时默认要求范围内结果完整；部分试跑显式加 `--allow-partial` 并呈报 `coverage.json` 中的未执行编号。提前停止或执行进程中途退出时保留原始报告，用 `report.partial.json`（模板逐条落盘）或 `report.json` 核对实际执行编号，用 `--allow-partial` 列明未执行范围；不把整个批次转换成跳过或受阻。提交回输入的原工作区与批次，随后读取看板核对编号、结果和已执行数；不自行新建批次或把子步骤通过等同整条场景通过。未提供 scope 时不宣称已校验批次和内容指纹。回归沉淀见下文。

## 受阻与失败细则

| 情况 | 处理 | 契约状态 |
| --- | --- | --- |
| 预期不符、禁止的副作用发生 | 让断言失败 | `fail` |
| 连接/录屏准备失败、前置条件不满足、人机验证、登录过期、来源不明的遮挡 | `throw new Error('受阻：<原因>')` | `blocked`，需问题编号 |
| 场景本轮不执行 | `test.skip()` 并说明 | `skip`，需问题编号 |

先确认操作确实作用于正确的可见对象与容器。定位依据失效、错误容器或连接故障记为带原因的执行受阻，不冒充产品缺陷；正确操作后的业务结果不符仍为失败，不能把所有超时都改成受阻。清理和附件登记失败必须留记录，且不能覆盖原业务错误。

不要为了跑通把受阻改成跳过或通过；也不要自动关闭来源不明的遮挡层，它可能正是要发现的缺陷。

**重试与止损**：只重跑没通过的场景，业务失败最多再确认一次。单个场景累计 15 分钟或 4 次仍无结论时，记下现象和证据转下一个：判断不了记受阻，证据表明产品不符记失败。

## 回归沉淀

浏览器执行脚本和界面地图保留在仓库外的看板任务目录，后续按场景重新执行。用户另行要求把已确认且经真实入口通过的场景沉淀为产品测试时，使用随包 [regression-distill](../regression-distill/SKILL.md)，由它调查已有设施、按成本分层、运行并登记关联，改动留给开发者审核。只有产品已有端到端设施且业务需要时才编写浏览器回归。入口仍由工作区 config.yaml 声明，不新增 checks 或场景 regression 字段。依赖已登录 Chrome 的脚本只在本机运行，如实报告实际执行与独立性。

## 任务与工作区约定

看板自动任务的 scope 只列本轮 Web 场景；混合入口最终 results.jsonl 还须合并其他驱动的结果，覆盖总 pending。修复复测按 playwright-scopes.json 分批转换，保留原 batch，避免同编号跨批次混用。

若任务来自 `sop-board execute prepare`，也只在返回的任务目录写结果和证据，再由 `execute submit --task <编号> --implementer <实际实现者>` 统一登记，不另跑 playwright submit。独立的手工试跑仍可用上述提交命令，并可加 `--implementer`；当前实现会话执行须如实登记自检，未给实现者则为未声明。

当前工作区版本使用 config.yaml 声明入口，sop-board guide 是工作区搭建指引；playwright guide 为 Web 驱动指引。手工 scope 用 --workspace，既有 project 字段存工作区键，提交走 /api/v1/workspaces/。工作区的 source-changed 沿用和跨仓修复范围以任务提示为准，修复复测不得沿用。
