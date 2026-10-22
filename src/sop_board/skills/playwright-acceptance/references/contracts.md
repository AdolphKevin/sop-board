# sop-board 验收契约

本技能随 sop-board 发行，不依赖其他流程工具。看板校验器是字段约束的事实来源。

## 场景

`scenarios.yaml` 顶层为 metadata 与 sops；metadata 包含 requirement、batch、version、owner、sources。每个 SOP 节点有 id、title、actor、entry、scenarios；场景包含 id、polarity、title、actor、priority、verify、initial_state、steps、expected、forbidden_effects，反向场景另有 negative_type。场景业务预期由上游确认，不由浏览器驱动改写。

完整场景文件需正反向覆盖；看板为本次任务筛选出的文件可能只含待执行的某一方向，不能为补结构而新造场景。任务的 source/scope/snapshot 和原始场景版本由看板固定。

## 结果

results.jsonl 每行一个场景：scenario、status（pass/fail/blocked/skip）、actual、executed_at（带时区）、executor、evidence（相对结果目录的文件路径数组），可选 session、finding、note。pass/fail 至少一份证据，blocked/skip 须有 finding。单个证据文件不超过 64 MiB，路径不能越出结果目录。

自动执行：`sop-board playwright convert --out <任务目录>`，证据放在任务目录 evidence/ 下。看板校验结果完整性后，从任务快照补内容指纹、执行者、产品版本并登记轮次；执行者不自行调用 submit，以免重复登记。修复复测结果另有 batch 字段，按 playwright-scopes.json 分批转换、汇总后交回看板。

手工执行：先用 `sop-board playwright scope` 固定原文和场景指纹，用 `convert` 生成结果，再用 `submit` 通过 `/api/v1/evidence` 与原工作区/批次的 `/rounds` 登记。submit 要求看板已存在同一原文版本，拒绝跨版本提交；SOP_BOARD_URL 通过环境提供；工作区 API 无需令牌，不放进报告。它不创建工作区或批次，也不自动猜测产品快照。

## 配对标签

pairs.json 顶层为 covered、conflict、disposed 数组。covered/conflict 含 case、scenarios、note；disposed 含 case、state、reason、confirmed_by。`sop-board playwright tags` 仅把已确定的对应关系转成 Playwright tag，不生成业务预期，也不把标签当作通过结果。

## 回归

场景仍是验收单位，不接受场景 regression 字段或 checks 声明。用户另行授权的测试建设使用随包 [regression-distill](../../regression-distill/SKILL.md)，通过 `sop-board distill submit` 登记测试关联；关联不写场景执行轮次或改变验收结论。入口来自工作区 config.yaml；既有历史格式的说明不代表恢复旧接口或测试套件。

当前入口唯一配置为工作区数据目录 config.yaml；scope 的兼容 project 字段存工作区键，手工 scope 使用 --workspace。旧 .tw 字段说明只用于理解历史契约，不恢复配置读取或令牌逻辑。
