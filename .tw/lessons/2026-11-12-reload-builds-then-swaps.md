---
title: "守护进程里共用的配置对象重新读取时先在局部算好，再一次换上"
scope: ["self:src/sop_board/config.py", "self:src/sop_board/server.py", "self:src/sop_board/envs.py"]
source: ["统一验收 2026-11-12 ENV-02-P01 步骤 1：main 与 wt1 同时 run prepare，5 次里 4 次被以“项目配置不合格”拒绝"]
verified: {"self": {"commit": "f6a021cc16d990c96529af290b929dcc7937915b", "date": "2026-11-12"}}
guards: ["self:docs/sop/environment.yaml#ENV-02-P01"]
tags: ["并发", "run prepare", "env up", "配置不合格", "同一仓库不能重复声明", "须为已声明的仓库别名", "reload", "守护进程", "线程"]
---
# 守护进程里共用的配置对象重新读取时先在局部算好，再一次换上

现象：同一项目的 main 与 wt1 同时运行 `run prepare`，一方或两方返回“项目配置不合格：repos.app/lib：同一仓库不能重复声明”或“scenarios.repo：须为已声明的仓库别名…”。config.yaml 没变，单独运行都成功。

根因：守护进程的请求线程共用一个 `config.Project`。`envs.Manager.up`（`run prepare`、`env up`、`check` 都经它）每次调用 `project.reload()`，旧实现先把 `self.repos` 等清空成新字典，再逐个仓库跑 git 校验后填进去。两个线程交错时，一个线程往另一个线程刚清空的字典里检查“是否重复”，或在别的线程清空之后读 `self.repos` 找场景仓库别名；`self.shared` 也会暂时为空，让业务记录短暂落到本机库。

修复与防护：`Project.read()` 只在局部变量里读取和校验，返回全部字段；`reload()` 在锁里用一次 `__dict__.update` 换上。以后凡是守护进程里共用的对象（项目、App、各 Manager），重新读取都先算好再整体替换，读的一方任何时刻看到的都是一份完整的配置。

发现方法：两个检出同时 `run prepare` 连跑 20 次；或并发 `env up` 与看板接口，每轮先改 config.yaml 的修改时间，迫使 `App.reload` 也同时重读。

适用条件：改动 `config.Project`、`server.App.reload`，或给守护进程新增会在请求里重新读取的共享状态时。
