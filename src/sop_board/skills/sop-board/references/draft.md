# 会话草稿

先完成入口技能的 status 检查，再按 [sessions.md](sessions.md) 记录本会话身份。

1. 确认工作区已登记、配置合格，批次已登记业务 SOP 原文。缺原文时请使用者提供文件，并在本次请求中指定上传人和确认人，再运行 `sop-board sop upload <工作区> <批次> <文件> --uploaded-by <姓名> --confirmed-by <姓名> --session <标识>`。此命令登记已有原文并认领草稿任务；只有需求资料、需要先编写 SOP 时读取 [author-sop.md](author-sop.md)。
2. 运行 `sop-board draft prepare <工作区> <批次> --session <标识>`；自定义数据目录加 `--data <目录>`。读取返回的提示词，按其中契约在返回目录写最终输出，然后运行 `sop-board draft submit <工作区> --task <编号>`。
3. 呈报草稿和待确认问题，请使用者答复时一并写明答复人姓名。批注与业务答复使用使用者给出的原文和姓名：`sop-board draft annotate <工作区> <批次> --version <版本> --target <目标> --kind <类型> --text <原文> --author <姓名>`。回答问题用 `--kind answer --target question:<问题编号>`，删除用例用 `--kind remove --target case:<用例编号>`，其他意见用 comment 或 add 指向 draft、case:、rule: 或 question:。按提示修订时再次 prepare，不自行补充业务口径。
4. 确认人必须由使用者在本次确认请求中明确指定。此前的自我介绍、上传人、答复人或其他角色姓名都不能代替；未给出时先询问。然后运行 `sop-board draft confirm <工作区> <批次> --version <版本> --confirmed-by <姓名> --session <本会话标识>`，只认领随确认产生的合并任务，避免看板替会话自动执行。
5. 确认成功后先呈报告知并询问是否继续合并，等待使用者决定，不自行运行 merge prepare/submit。使用者在当次请求中已明确要求继续合并的，沿用该明确授权；同意继续后才读取 [merge.md](merge.md)。

输出校验失败时，修正同一任务目录的输出并再次 submit；任务已作废则按最新输入重新准备。排队冲突先呈报编号，仅在使用者同意替换时加 `--replace-queued`；已有授权沿用，不重复询问。运行中或其他会话认领的任务不接管。参数和目标格式以各命令 `--help` 为准。
