# 合并测试用例

## 上传

使用者给出用例文件时：`sop-board inbox add <文件> --kind cases`。格式不合格时把输出列出的字段与位置转告使用者。测试同学也可在看板收件箱上传。`sop-board inbox list`、`sop-board inbox show <编号>` 查看待合并条目。

## 合并

1. `sop-board merge prepare [--inbox <编号> …]`（缺省为全部待合并用例），读输出的 prompt.md 并按其执行：直接编辑仓库中的场景与用例文件。
2. 有疑问时：
   - 使用者能回答的，在对话中问。
   - 需要测试同学回答的，`sop-board ask --about inbox:<编号> --text "<问题>"`（关于某条场景用 `--about <文件>#<场景编号>`），告诉使用者问题已挂到看板“待答复”，本次合并停在这里。测试同学答复后，`sop-board answers [--unanswered]` 查看；再次 `merge prepare` 时材料里带有答复。
3. `sop-board merge submit`：校验全部场景文件，通过后输出新增、移除、改动的场景编号及角色、入口变化，并把收件箱条目标为已合并。不合格时按列出的文件、字段与行号修正后重交。
4. 把差异转述给使用者，`references_only`（只加了引用、四要素没变的场景）按 prompt.md 逐条处理并说明。改动是未提交的仓库文件，由开发者审核提交。

使用者决定不合并某条目时：`sop-board inbox reject <编号> --note <理由>`。
