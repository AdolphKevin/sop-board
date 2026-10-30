# 问题台账、决定记录与用例回写

先完成入口技能的 status 检查。以下命令都可加 `--data <数据目录>`；`--by` 为使用者给出的姓名，设置了默认业务确认人时可省略。

## 问题台账

问题台账替代手写的 markdown 台账，看板批次页「问题台账」只读展示。

1. 新问题：`sop-board issue add <工作区> --title <标题> --kind <product|scenario|environment|tool> [--scenario <批次>#<场景> ...] [--root-cause <根因>] [--note <原话>] [--by <姓名>]`。标题写业务可读的现象，根因未确认时先不填。
2. 执行中遇到已登记问题时，在 results.jsonl 对应行加 `"issue": <编号>`，登记轮次时归并；编号不存在会被拒收，先新建再提交。
3. 修改状态、根因、Jira 或影响场景：`sop-board issue update <工作区> --issue <编号> [--status <open|deferred|closed>] ... --note <使用者原话或理由> [--by <姓名>]`。暂缓、关闭须是使用者的决定，`--note` 写其原话。修复中、待验证、已关闭会随关联修复自动显示，不手工设置。
4. 按问题修复：`sop-board fix prepare <工作区> --issues <编号> ... --session <修复会话标识>`，之后按[修复与独立复验](fix.md)进行。
5. 查看：`sop-board issue list <工作区> [--batch <批次>] [--open]`、`sop-board issue show <工作区> --issue <编号>`。

## 决定记录

使用者对业务口径、范围、暂缓等给出决定时，登记原话：`sop-board decision add <工作区> [--batch <批次>] [--issue <编号>] --text <原话> [--by <姓名>]`。`sop-board decision list <工作区> [--batch <批次>]` 汇总会话登记的决定、问题台账修改、界面对照决定和合并答复。不把自己的建议登记为决定。

## 默认业务确认人

只在使用者要求时设置：`sop-board confirmer <工作区> --set <使用者给出的姓名>`；`--clear` 取消。设置后在呈报中说明各项署名将使用该姓名。

## 用例导出与回写

合并答复改写过用例后，测试同事的原文件不会自动变化。

1. 导出：`sop-board cases export <工作区> <批次> [--upload <上传编号>] --out <新文件>`，按该次上传的用例与顺序输出最新内容，`changed` 列出改写过的编号；SOP 草稿确认生成的用例用其上传编号单独导出。
2. 回写：使用者同意后 `sop-board cases export <工作区> <批次> [--upload <上传编号>] --write-back`。只写回会话上传时记录的原文件；文件在上传后被改过、或位于计入产品快照的仓库路径时会被拒绝，按输出说明处理，不改为直接覆盖。回写重新排版、不保留注释，先告知使用者。
