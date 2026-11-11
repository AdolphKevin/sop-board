# 编写业务 SOP

## 从需求资料编写

1. `sop-board sop prepare --source <资料> [--source <资料> …]`：复制资料并输出 prompt.md。按它先核对场景目录中已有 SOP 的业务交叉，再写 SOP Markdown；资料矛盾、没写明的业务结果或改变已有规则的口径，在对话中问使用者。
2. 按答复改正文，直到没有未答的问题。
3. `sop-board sop submit --file <场景目录下的 .md>`：校验六个标题与规则编号，并在文末追加确认信息（确认人为操作者；使用者说明是别人确认的，加 `--as <姓名>`）。不合格时按输出补齐后重交。
4. 报告 SOP 文件位置、确认人与业务交叉清单；看板的 SOP 原文页可查看。使用者要继续生成验收场景时读 [draft.md](draft.md)。

## 登记已有 SOP 原文

1. 原文来源：使用者给出的文件；或收件箱中 kind 为 sop 的条目，用 `sop-board inbox show <编号>` 取原文另存为资料文件。使用者要求先上传时，`sop-board inbox add <文件> --kind sop`。
2. 以原文为资料走上面的步骤 1～3：业务内容只取原文，按 prompt.md 的六个标题组织；原文缺少某部分的内容时问使用者。原文是别人确认的，submit 加 `--as <姓名>`。
3. 来自收件箱的，登记后 `sop-board inbox done <编号>`。
