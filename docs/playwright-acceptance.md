# 内置 Web 验收

技能、模板、转换器及既有资源统一维护于 `src/sop_board/skills/playwright-acceptance/`，随包发行。业务验收遵循[统一标准](standard.md)，项目操作、账号、观察与清理按产品仓库真实验证手册执行。

## 安装与入口

```sh
uv tool install /path/to/sop-board
sop-board guide
sop-board skills install
sop-board playwright guide
```

`guide` 保留 main 的工作区搭建指引并列出 Web 入口，不启动服务。`playwright guide` 只显示 Web 操作。默认使用包内技能；显式 SOP_BOARD_SKILLS_DIR 下存在 playwright-acceptance/SKILL.md 时可覆盖。自检使用同一解析规则，其余驱动遵守工作区配置。

## 看板执行与修复复测

看板执行任务提供 scenarios.yaml、总 pending.json、Web 专用 web-pending.json、scope.json、entries.json 及快照。看板已准备适配环境，执行者按入口操作，不自行启停服务。Web 连接在同一执行进程共用浏览器调试连接（CDP），每条实际执行场景按入口 `evidence` 声明留证；`web_evidence.video`、`web_evidence.trace` 各自缺省开启，显式 false 时不启动对应录制、不要求对应附件。任务 scope 按场景保存开关，模板读取 `PW_SCOPE`；关闭本轮全部录屏时不检查 ffmpeg。配置例子见[参考手册](reference.md#配置与环境)。交互式 Chrome 模板使用 workers: 1、retries: 0、maxFailures: 1；首个意外失败或受阻后停止本轮，避免进程重启引发连续调试授权。剩余未执行范围单列，部分转换显式加 --allow-partial。

```sh
sop-board playwright convert --report <报告.json> --out <任务目录> \
  --scope <任务目录>/scope.json --scenario-file <任务目录>/scenarios.yaml \
  --pending <任务目录>/web-pending.json --executor <执行会话>
```

混合入口合并各驱动结果到同一 results.jsonl，覆盖总 pending。仅 source-changed 场景可按主流程规则沿用；Web 报告只包含实际执行项时，使用实际执行编号的 pending、显式 --allow-partial，并将沿用结果与报告结果合并后交回。不得漏掉未沿用场景。自动任务由看板登记，不调用手工 submit。

修复复测通过 playwright-scopes.json 按批次获取独立场景原文和 scope，分别转换后补 batch，证据路径统一相对任务目录 evidence/。修复复测不沿用原通过结果。

## 开发会话执行与独立复验

先读取 `sop-board status <工作区> --json` 的 setup_check.ok，核对最近已完成自检是否仍适用。latest_attempt.completed=false 的尝试不替换原有效结论。Codex 沙箱内不运行 check；缺少合格结果时请使用者在沙箱外完成。普通执行使用 `sop-board execute prepare <工作区> <批次> --session <执行者标识>`，默认交未参与实现的新会话；修复复验使用 `sop-board execute prepare <工作区> --fix <修复编号> --session <复验标识>`，范围由原修复记录确定。

会话入口从任务目录 repos.json 读取仓库路径，按项目验证技能与手册自行准备入口与隔离数据；结束前停止自己本轮启动的服务，在结果 note 中列名称、PID 或进程组及停止结果，清理失败如实列仍运行的进程，不停止使用者已有服务或浏览器；地址及声明凭据来自会话自身环境，账号授权沿用本任务记录。以 prepare 返回的提示词、origin.json 和 Web 材料为准，不能套用“看板已启动环境”的前置。写入权限包含数据目录；执行时产品代码仍只读。

转换命令与上节相同。已有 prepare 任务时只向任务目录写完整结果与证据，随后 `sop-board execute submit <工作区> --task <执行任务编号> --implementer <实际实现者>`；复验的实现者已绑定原修复会话，不另填或替换。不要再用 playwright submit 登记第二个轮次。普通执行中同一实现者和执行者登记为自检，未声明实现者为未声明；独立性字段不能代替实际独立执行证据。

## 手工登记

```sh
sop-board playwright scope --workspace <工作区键> --scenario-file <原文.yaml> --entries /work/entries.json --out /work/scope.json
sop-board playwright convert --report /work/report.json --out /work/round \
  --scope /work/scope.json --scenario-file <原文.yaml> --executor <执行者>
sop-board playwright submit --results /work/round/results.jsonl --scope /work/scope.json \
  --scenario-file <原文.yaml> --product /work/product.json \
  --idempotency-key <本轮唯一编号> --submitted-by <执行者> --url http://127.0.0.1:8787 \
  --implementer <实际实现者>
```

手工 scope 的 `--entries` 接受入口 JSON 对象或任务 entries.json；省略时沿用录屏、trace 均开启的默认行为。旧任务已复制的模板不会自动更新，重新准备任务后复制本版本模板。

工作区键使用页面与 API 中的实际名称，不把仓库身份转换为旧项目键。scope 的既有 project 字段保存该工作区键，--project 仅为兼容参数别名。看板须先有同一场景原文；submit 使用 `/api/v1/workspaces/<键>/batches/<批次>/rounds`，无需令牌，不跟随重定向，上传证据前核对原文版本。地址也可通过 SOP_BOARD_URL 提供。product.json 包含 commit、digest（兼容 product_digest/product_fingerprint）、dirty；多仓版本取当前工作区快照，不把 HEAD 当成运行态证明。

完整执行默认拒绝漏测；部分试跑显式 --allow-partial，并检查 coverage.json。保留旧数据目录：main 的 v9 版本不读取或迁移旧看板数据，不把手工登记当成迁移工具。

手工 submit 保留旧参数，--implementer 可省略，省略时独立性为未声明；登记入口为会话。pass/fail 必须有真实证据，blocked/skip 可用空 evidence 数组但须说明 finding。部分试跑成功只说明所交范围已登记，不代表整批完成。配对文件转为用例标签使用 `sop-board playwright tags --pairs <pairs.json> --out <cases.json>`。

## 验证与维护

用户要求把已确认且经真实入口通过的场景保留为产品回归测试时，另用随包 [regression-distill](../src/sop_board/skills/regression-distill/SKILL.md) 按成本选择测试层、核对实际运行并登记关联。浏览器验收脚本与界面地图保留在仓库外的任务目录；本技能不把它们直接复制为产品回归套件，只有产品已有端到端设施且业务需要时才由 regression-distill 编写浏览器测试。

遵循 main 的场景验收方式，通过实际命令、工作区 HTTP 入口与页面核对，不恢复上游删除的根 tests/。现有技能资源随包保留；发行包应包含技能、模板和转换器。提交后核对工作区、批次、编号、轮次、证据及进度，不以 Playwright 的绿色数量代替整批业务结果。

## 跨项目操作映射

通用技能只规定先识别状态、必要时导航、按完成信号等待和异常停止。页面名称、菜单、路由、iframe 特征、对象标识、写入口及成功信号写入项目 surface-map.md；模板不预设某个业务页面。

目标视图已满足前置条件时直接复用，不再次点击外层入口。定位失效先只读核对当前文档、弹窗及 frame；正确业务操作后的结果不符仍为失败，定位和连接问题单独记为执行受阻。保存或提交前注册响应/事件等待，核对业务成功的可见结果后才刷新或导航；状态不明时不重复提交。

仅更新技能说明及模板不会改写现有任务目录中的执行脚本。继续旧任务前须核对其项目映射、完成等待和停止配置；不因模板已修正就宣称旧轮次结果有效。
