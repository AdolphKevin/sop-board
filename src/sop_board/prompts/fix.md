# 修复 #$fix：在检出 $checkout 修复不通过的场景

工作目录：$directory。先读项目验证技能 $skill 与它引用的手册。

材料：problems.json（问题场景、最近结果、finding、缺陷链接与证据文件路径）、scenarios.yaml（场景原文，预期只来自这里）、repos.json（本检出各仓库的目录）。

## 要求

1. 依据证据和场景原文找出根因，判断是否为产品问题。环境、账号、前置数据、场景写法或业务变化都不是产品问题，不要用产品修改代替业务答复。
2. 产品问题：只在 repos.json 列出的本检出仓库里改产品代码。保留已有的未提交改动；不改场景文件，不写测试，不提交 git。
3. 在工作目录写 fix.json：

```json
{"root_cause": "<根因与证据>", "product_problem": true, "summary": "<改了什么、为什么能解决>", "notes": "<未覆盖或需要人确认的事项>"}
```

4. 运行 `sop-board fix submit --fix $fix`。产品问题进入待复验，提交输出给出复验命令。复验必须由未参与修复的新会话运行 `sop-board run prepare --fix $fix`，你自己不要复验。非产品问题会登记到待人处理。
