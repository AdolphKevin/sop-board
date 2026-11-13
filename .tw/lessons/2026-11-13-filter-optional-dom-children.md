---
title: "用原生 DOM 替换子节点前过滤可选空值"
scope: ["self:src/sop_board/assets/board.js"]
source: ["self:docs/sop/setup.yaml#SET-09-P01", "本机设置页浏览器自检（2026-11-13）"]
verified: {"self": {"commit": "e2593fba1f99d722bb7d4d47f20ffec3408cf921", "date": "2026-11-13"}}
guards: ["self:docs/sop/setup.yaml#SET-09-P01"]
---
# 用原生 DOM 替换子节点前过滤可选空值

现象：共享库测试成功后的产品清单出现一行“null”；接口清单本身正确，操作与保存也正常。

根因：页面的 el 帮助函数会忽略 null，但原生 replaceChildren 会把 null 转为文字。将可选节点的三元表达式直接传给 replaceChildren，空值就进入页面。

修复与防护：可选节点先交给 el 组成容器，或过滤空值后再传给原生 DOM 方法。两种方法不要混用空值约定。检查产品清单与凭据列表的空、非空两种显示。

发现方法：接口自检后仍用浏览器截图复核页面文字；同时检查连接失败后改正再测试，成功时清除旧字段错误，避免把旧原因留在新清单旁。

适用条件：为动态列表增加可选提示、空状态和重新渲染时。
