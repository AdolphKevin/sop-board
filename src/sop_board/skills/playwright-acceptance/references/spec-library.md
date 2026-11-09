# 规范脚本库、保真审核与只读巡检

D114～D118。工具、模板、转换器统一从随包的 `src/sop_board/skills/playwright-acceptance/` 发行，不读取其他检出的工具。

## 绑定与冻结

Web 入口可声明 `spec_library: web:acceptance/spec-library`，web 为仓库别名。该目录须位于仓库内且不能是仓库根；属于业务配置，新增/改绑须重新自检，已准备任务按配置变化规则作废。绑定应选在没有在途执行时设置，可能一次性改变产品摘要；之后库内容维护不改变配置摘要。

库按完整路径段自动排除产品快照，效果同 snapshot_exclude；相似前缀目录、产品代码与配置照常计入。经审核、受版本管理的规范执行资产可以放进产品仓库；任务冻结副本、审核材料和过程证据仍在仓库外。它不改变 regression-distill 的回归建设职责。

INDEX.json 为 `{"version":1,"entries":[...]}`，同批次同场景只有一个当前版本：

| 字段 | 内容 |
| --- | --- |
| batch、scenario | 批次、场景编号 |
| scenario_fingerprint、case_fingerprints | 场景指纹；引用用例编号到正文指纹 |
| spec、spec_sha256、deps | specs/<批次>/<场景>.spec.mjs 及摘要；全部本地依赖的库内相对路径到摘要 |
| version、baseline | 当前版本号；baseline/<批次>/<场景>/<版本>/ |
| state、origin | verified / reviewed-failing / unverified；promote / import |
| provenance | 来源任务、运行与导入依据 |
| review | {by,kind:person或session,session,at,candidate_sha256}；历史导入为 null |
| run | {batch,round,result_id,status,product_digest}；未知为 null |
| support | [{id,path,enabled:false}]，path 指向含 manifest.json 的辅助目录 |

收入时每版依赖放到 `pages/<批次>/<场景>/<版本>/`，保留相对导入，使其他场景的当前版不被公共依赖维护悄悄替换；基线镜像包含本版脚本与依赖，旧版保存在 `_history/<批次>/<场景>/<旧版本>/`。库脚本只从同目录 ./fixtures.mjs 和 ./library.mjs 导入，后者导出页面对象与辅助代码。不能使用共享绝对路径加载或计算的 import/require。解析器使用当前工作区安装的 Playwright；可在数据目录安装 @playwright/test >=1.64，其他安装位置用 PW_NODE_ROOT 指向相应项目目录。

普通执行与修复复验在最终范围冻结后写 `specs-manifest.json`，四类 seeded / missing / reference / conflict 都带原因。只预置同批次、同编号、同场景指纹且 verified 的版本；仅引用用例指纹不同仍预置并注明。缺失或索引未覆盖的依赖不能预置；脚本/依赖冲突保留原文件并逐个列出受影响场景。重复准备保留原候选和清单。

脚本在 scenarios/<场景>.spec.mjs，完整依赖在 scenarios/library/，scenarios/library.mjs 只导出冻结副本；运行器也补齐到任务内。辅助 manifest.files 中 role 为 spec/script/entry 的入口，以及辅助目录的 .spec.mjs、.py、.sh，以 .off 停用。启用时去后缀并在结果 note 说明。运行器只匹配以 .spec.mjs 结尾的文件。

模板每次开跑写 `spec-runs/<运行编号>.json`，固定 scope、脚本、直接/间接本地依赖、动态读取的 library/pages/support/data 资产、运行器与外部包摘要；二进制按原始字节计算。夹具写报告注解，convert 核对并复制摘要，以结果行 `spec_run:{id,file,candidate_sha256}` 关联。登记时写 playwright_result_assets 侧表，完整材料只在本机。复用仍须实际运行并留新证据，执行者为本轮实际运行者；复用的实际通过/失败缺少运行摘要会被拒收。录制只取本轮冻结 scope。旧任务没复制新增 spec-assets.mjs 时保留旧执行流程，但不能借旧报告收入新库。

## 保真、标识与账本

```sh
sop-board playwright specs check --task <任务> --scenario <批次>#<场景> --format json --data <目录>
sop-board playwright specs check --library <库目录> --format json
sop-board playwright specs review --task <任务> --scenario <批次>#<场景> --out <库外材料.json> --data <目录>
sop-board playwright specs promote --task <任务> --scenario <批次>#<场景> --reviewed-by <实际姓名> [--reviewer-session <实际会话>] [--state verified|reviewed-failing] --data <目录>
```

check 只读，--scenario 可多值或重复。JSON 的 reports 各含 same / review / violation / no_baseline；每项提供 assertion_id、old/new（原文、对象、匹配器与参数、计算来源、步骤、条件、摘要和文字节点）、reason、精确匹配的 ledger。有违规时非零退出，其余三类不是业务通过。对象、计算、等待成功/失败等匹配附加条件、条件或步骤变化都要审核；执行结构、等待关系、方法归属或断言前的控制流程变化无法证明等价时保守列待审核。首次无基线逐条列出全部断言，不伪装相同。

稳定标识写在语句前 `// sop-assert: A_ROLE`，跨文件移动时保留，同一场景内不能重复。没标记时输出 `auto:<逻辑文件>:<类别>:<序号>`，新增/删除会影响后续序号，维护宜使用显式标识。SQL 字面量单列，显式标识追加 :sql。node_path 是 signature 内的 JSON Pointer；执行者必须复制 check 输出，不手推路径。

例如以下断言的两个 Member 分别位于 /subject/arguments/0/arguments/0/value 与 /args/0/value：

```js
// sop-assert: A_ROLE
await expect(page.getByText('Member')).toHaveText('Member');
```

只改第二个文字节点时，任务 `spec-ledger.json` 可写：

```json
[{"id":"role-copy-1","batch":"spec-demo","scenario":"WEB_OK","assertion_id":"A_ROLE","node_path":"/args/0/value","old":"Member","new":"成员","reason":"引用已确认的同义文案变更，定位文字保持 Member","ref":"实际确认资料位置"}]
```

其他断言和同一断言的其他同文案节点仍须分别记账。文字记录不能批准对象、条件或结构变化。结构条目为 `{id,batch,scenario,assertion_id,kind:"rewrite",old,new,reason,ref,sop_change:null}`，old/new 复制 check 的完整原文或 signature。新增断言与首次无基线项目由审核者逐条核对。扩大正向文字接受范围会明确提示，必须具体说明理由；空理由或笼统“跟随 UI”不能批准。

撤销用 `kind:"withdraw",new:null`，sop_change 为 `{case,before_fingerprint,after_fingerprint,decision_id}`。工具核对本工作区、本批次的真实 cases put 决定、对应上传号与用例前后正文摘要；旧摘要须对应基线引用用例。本机理由、别的用例决定或任一错误摘要都仍是违规。

review 只生成材料，不产生已审核结论。未参与本次改写者实际核对差异、每条去向与真实运行后再 promote 署名；会话审核不能等于任务执行或实现会话，人审核只填实际姓名。收入先按运行摘要核对并冻结候选，再从副本检查和收入；原稿后续编辑不会混入本版。候选或依赖与本场景最近运行不同、最近运行未登记、状态不符、有违规、待审核项缺精确账本或账本未覆盖变化都会拒收。verified 要已登记通过；reviewed-failing 要已登记失败且有 finding。收入把账本连同改写者、审核者、时间和候选摘要并入两份库账本，推进基线并归档旧版；首次新写同样建基线。工具不能证明真人参与关系，必须如实记录。

## 只读巡检

```sh
sop-board playwright patrol plan --entry app [--atlas <地图.json>|--surface-map <地图.md>] --out <新计划.json> --data <目录>
sop-board playwright patrol run --entry app [--plan <计划.json>] --out <新库外目录> [--cdp <已有浏览器地址>] [--tab <唯一URL子串>] --data <目录>
```

plan 缺省读取已登记 atlas；文字 surface-map 保留原行和未确定项，也可提供 patrol-map 代码块（含 pages 数组、id/route/controls/tables 定位）。生成物一律未审核，未知请求用途仍待补齐。

patrol.json 为 `{version:1,entries:[...]}`。每项包含 id、entry、identity、source、page/controls、pages、reach、targets、allow_requests、forbidden_actions、review、uncovered。pages 为允许路径模式，* 匹配单段、** 多段，缺省限定入口同源；跨源须写完整 http(s) 地址模式。allow_requests 为 `[{method,path,purpose}]`，用途不能仍是“待审核”；GET 也可能写入，不能整类放行。

reach 仅支持 `{action:"goto",path}` 或 `{action:"click",locator}`；targets 为 `[{ref,locator,count}]`，count 为正整数，全部匹配都须可见，不用 first 掩盖数量错误。locator 支持 css、testid、role/name/exact、text/exact，可加 frame；支持地图的简单 locator/getByTestId/getByText/getByLabel/getByPlaceholder 表达式，不执行任意 JavaScript。

review.by 与 at 由实际审核者填写；可附 plan_sha256，绑定移除 review 后、递归按键排序的紧凑 JSON 摘要。identity 引用入口已声明身份，调试地址取其 cdp 或 --cdp/PW_CDP_ENDPOINT，不登录或切换账号。

run 在已有登录 context 内创建本轮专用页，首次导航前安装拦截并绕过该页服务工作线程缓存。document/script/stylesheet/image/font/media 直接放行，其他请求按 method/path 声明。未知 fetch/xhr 在到达服务端前中止并停止条目；WebSocket 在连接服务端前核对。所有导航仍须符合 pages，包括跳转与子框架；静态 document 不豁免页面边界。未知弹出页停止。识别只到地址与请求，不识别业务对象实例；项目仍须审核允许请求的实际只读语义，并逐值核对完整业务数据前后不变。

巡检开跑读取入口当前 web_evidence，写 patrol-input.json 与 patrol-report.json，随后本次固定。video/trace 分别缺省 true、分别关闭；失败始终尝试保存 failure.png，页面不可用时明确记录不可得原因。定位缺口写 atlas-feedback.jsonl，任一失败非零，未审核项列未执行。巡检不登记场景结果、不清除待判定、不作为沿用依据。

## 历史导入与材料边界

`playwright specs import --from <accept-run目录> --batch <批次> --data <目录>` 首版只读 cw2 结构：lib/specs/INDEX.json 的 scenarios 数组、规范脚本、snapshot 指向的 _source/<编号>/manifest.json 与来源脚本、lib/pages、辅助资产和两份数组账本。旧字段为 scenario、canonical、source_task、source_file、run/runs、result、可选 latest_result、versions、snapshot、support、pages。manifest.source.spec 指来源快照文件，snapshot_sha256 校验快照；不跟随外部绝对 source 路径。旧账本用 spec 关联，保留正文并标“历史导入，未经本包审核”，不执行历史工具或外层调度。

来源最近通过、规范与来源快照字节相同时为 verified/import；改写过的及其余为 unverified/import。保留来源任务、运行、来源与规范摘要及改写事实。指纹只取目标批次、本场景的来源登记结果及来源版本配对；旧 r01 不是看板第1轮，按来源任务登记收据和执行时间关联。不用当前场景、外部自填指纹或其他批次同编号补齐。未知或当前指纹不同都只参考；导入不产生通过。重复覆盖当前条目拒收，须先核对或走正常收入。

库、基线、历史、账本、差异、清单和巡检材料只在本机。任务附件仍按既有服务端白名单控制，新增原文不向内网共享；新增投影只有复用数量、审核结论和审核人姓名，原有轮次业务身份保持原口径。资产以文件保存，新增运行绑定和审核侧表不改变格式5或旧表列。


## 返工1补充边界

普通脚本仍可按原指引导入 atlas.mjs、其他本地辅助文件或既有模块，运行摘要不施加规范库的双入口限制。仅预置规范脚本以及 specs check/promote 候选要求只经 fixtures.mjs/library.mjs 导入。普通脚本无法静态解析的动态加载记录为 unresolved，不能把该摘要当作规范资产全部依赖已冻结的证明。

巡检对HTTP响应关闭自动重定向，逐跳先核对方法、地址和用途，再发下一跳；同源fetch/xhr可继续已声明的链。跨来源重定向和页面导航重定向不能安全保留浏览器的来源、凭据或实际地址语义时，下一跳不发出并按失败处理，应改用经审核的最终直达页面。请求链总时限30秒、最多20次跳转。截图、停止录制、关闭页面及在途请求收尾后还要核对违规标记，清理期间的未知请求同样使整次非零并留截图、原因和地图反馈。

断言签名保留本文件前置断言的稳定标识及顺序；交换顺序或改变后续执行上下文会待审核，可能增加需要逐项核对的范围。显式latest_result时间缺失、非法或与历史顺序冲突时保留来源说明并标未核实，不回退借旧通过。

分批复验按结果批次和场景在playwright-scopes.json及本轮playwright目录中唯一定位冻结范围。缺目录、缺清单、重复归属均拒收；缺spec_run门槛和库版本/复用关联使用同一份子目录清单。非Web结果仍按原驱动登记，不套用Web分批范围。
