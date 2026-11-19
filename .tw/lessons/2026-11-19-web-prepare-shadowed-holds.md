---
title: "准备运行时局部变量覆盖卡住场景参数，含 Web 场景的 prepare 必崩"
scope: ["self:src/sop_board/runs.py"]
source: ["cw2 看板 2026-11-19 启动 #140 的 run prepare 崩溃记录（TypeError: string indices must be integers）", "self:src/sop_board/runs.py"]
verified: {"self": {"commit": "2b45f512ad713f4b17208b5a47fb3cf48199cb16", "date": "2026-11-19"}}
tags: ["run prepare", "materialize", "write_brief", "卡住场景", "变量遮蔽", "真实冒烟"]
---
# 准备运行时局部变量覆盖卡住场景参数，含 Web 场景的 prepare 必崩

2b45f51 给 `materialize` 新增关键字参数 `held`（点名执行的卡住场景，字典列表），但函数里 Web 分支原有的局部变量也叫 `held`（本轮取得的浏览器身份名集合）。范围里只要有 Web 场景，局部变量就覆盖参数，`write_brief` 把身份名字符串当卡住场景读 `item['key']`，抛出 `TypeError: string indices must be integers`，运行没有建立、身份也未租出，启动显示 launch-no-work。只含命令行场景的范围不经过该分支，所以假客户端与临时库的自检发现不了。

修法：Web 分支的身份集合改名为 `leased`，参数 `held` 原样传给 `write_brief`。cw2 看板在已安装包上打同一补丁后，含 Web 场景的启动 #141、#142 均正常准备并提交。

改动 `prepare`、`materialize`、`write_brief` 这条链路的参数时，检查函数体内有没有同名局部变量；交验收前至少用一个含 Web 场景、且有卡住或处置场景被点名的范围做一次真实 `run prepare` 冒烟，不能只用替身客户端。
