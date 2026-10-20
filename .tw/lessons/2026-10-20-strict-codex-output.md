---
title: "合并输出必须满足严格对象模式"
scope: ["self:src/sop_board/prompts/**", "self:src/sop_board/drafts.py", "self:src/sop_board/tasks.py"]
source: ["self:docs/verification.md"]
verified: {"self": {"commit": "119c05fcbe8bfc239d99b14dd0f379d4374ec3c6", "date": "2026-10-20"}}
---
# 合并输出必须满足严格对象模式

现象：上传用例后，真实 Codex 合并在创建输出前拒绝模式，提示 cases.items.required 缺少 initial_state。仅显示退出码时，页面无法说明原因。

根因：普通 JSON Schema 的可选键和任意键映射不能直接用作 Codex 严格输出模式。每个对象必须禁止额外键，required 必须包含 properties 的全部键。

修复与防护：可选字段仍列入 required，类型允许 null；含枚举的字段也在枚举中允许 null。任意键的 relocate 映射改为固定字段数组。在唯一的合并输出读取入口先核对原始模式，再移除可选 null 并转换映射，后续业务继续以键是否存在判断。非零退出从 error 和 turn.failed 事件提取最多 300 字摘要，保留退出码。

发现方法：Claude 经真实上传入口触发合并，从 events.jsonl 发现服务端模式拒绝；逐层静态核对全部随包模式发现 relocate 同样不合格。修改模式时做一次性静态核对，再由能够运行真实 Codex 的独立验收者确认合并；静态核对不能代替合并成功证据，不新增测试文件。

适用条件：增加或修改任何随包结构化输出模式，尤其是可选字段、枚举、嵌套对象和动态映射时。
