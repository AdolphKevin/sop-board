---
title: "页面改查询参数时整段重新赋值地址，# 片段放在查询参数之后"
scope: ["self:src/sop_board/assets/board.js"]
source: ["实现决策 19、20 时的浏览器自检（2026-11-12）：在工作台打开草稿文件面板后用检出切换选 wt1"]
verified: {"self": {"commit": "5c06732e67a0171fdc1b689824aed6e4430f2425", "date": "2026-11-12"}}
guards: ["self:docs/sop/authoring.yaml#AUT-02-P02", "self:docs/sop/authoring.yaml#AUT-02-N03"]
tags: ["检出切换", "地址", "location.search", "hash", "分享链接", "工作台"]
---
# 页面改查询参数时整段重新赋值地址，# 片段放在查询参数之后

现象：工作台右栏打开着某个场景（地址带 `#shop#SHOP-W1-P01`）或草稿面板（`#review:draft-b`）时，用左栏的检出切换选 wt1，页面报“检出不存在：wt1#review:draft-b”，左栏为空。

根因：检出切换写的是 `location.search = search + location.hash`。赋给 `location.search` 的 `#` 被编码成 `%23`，片段并进了 `checkout` 参数。只在地址带 `#` 时出现，从概况页切换检出（没有 `#`）一切正常，所以之前的验收没碰到。

修复与防护：用 `location.assign(location.pathname + '?' + search + location.hash)` 整段赋值，切换后仍打开原来的场景或面板。以后凡是改查询参数，都整段重新赋值地址，不往 `location.search` 里拼别的部分。

发现方法：带 `#场景键` 或 `#review:` 面板地址时切换检出，看地址里的 `checkout=` 是否带 `%23`。

适用条件：改动工作台、业务 SOP 页的地址与检出切换时。
