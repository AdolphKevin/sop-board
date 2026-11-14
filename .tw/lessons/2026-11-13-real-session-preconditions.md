---
title: "启动会话前共用范围检查，首次沉淀按场景找验收基线"
scope: ["self:src/sop_board/launcher.py", "self:src/sop_board/runs.py", "self:src/sop_board/guards.py", "self:src/sop_board/browsers.py", "self:src/sop_board/specs.py", "self:src/sop_board/skills/playwright-acceptance/scripts", "self:src/sop_board/playwright.py", "self:src/sop_board/cli.py"]
source: ["用户确认的 2026-11-13 真实会话独立验收问题修复要求", "self:docs/design.md", "用户 2026-11-13 返工 1 的五项审核意见", "用户返工 2 Y-D1 与独立复验 Y/ATS-02-N02", "使用者 2026-11-13 两项决定（返工 3）"]
verified: {"self": {"commit": "36c885d8fc7a471b5f5e005905afc94e8b06b389", "date": "2026-11-14"}}
guards: ["self:docs/sop/run.yaml#RUN-04-N02", "self:docs/sop/run.yaml#RUN-04-N03", "self:docs/sop/run.yaml#RUN-04-N04", "self:docs/sop/atlas-specs.yaml#ATS-02-N02", "self:docs/sop/atlas-specs.yaml#ATS-02-N03", "self:docs/sop/browser.yaml#BRW-05-N01"]
tags: ["启动范围", "基线", "真实路径", "浏览器退出", "独立验收"]
---
# 启动会话前共用范围检查，首次沉淀按场景找验收基线

启动器与 run prepare 共用范围检查，先拒绝不存在、草稿、指名场景所在文件不合格及已有 prepared 运行，再创建启动记录。pending/all 只选择合格文件的场景；输出与运行说明列出未纳入的不合格文件，空范围仍为 nothing-to-run 并附文件问题。进程以零退出不代表做了验收：公开启动结果还要检查关联的运行或沉淀，只有正常退出且无关联工作的启动显示未产生运行，wait 返回 launch-no-work；失败启动保持 failed、附本机日志末尾，wait 返回 launch-failed。正常退出但有关联 prepared 或 abandoned 工作时，列出未提交或已放弃的工作、附本机日志末尾，wait 返回 launch-unsubmitted；失败启动仍优先返回 launch-failed。上述失败 wait 均以退出码 1 返回，不自动提交工作。已定决策 29（2026-11-14）起，run 类型的启动异常退出时先接续一次；最终失败或正常退出却留下 prepared 的运行时，看板放弃该运行并登记待人处理（见 decisions/2026-11-14-unattended-hardening.md）；沉淀启动与被 launch stop 停止的启动照旧不自动放弃。沉淀准备须保留启动关联。

首次沉淀用验收报告里场景标题首词或场景标签与脚本文件的对应关系找基线，文件名变化不取消比较。本机没有运行目录记录或目录已不在时，不论产出机器是谁，都不设基线，只做静态检查，输出 no_baseline=true 与原因。目录在本机时，报告缺失或不可读、基线零份或多份仍拒绝。基线不合导入规则时指出基线文件，重新验收后再沉淀。

客户端目录解析为真实路径；运行时提供只读浏览器和 Node 依赖，产物写本轮运行目录，无需运行时写权限。

浏览器租约持有管理锁时会等待启动结果；退出监视线程读取已知身份的状态时直接查询本机库，避免再次进入需要管理锁的实例查找。先记录退出信息，再登记可能等待共享库的待人处理，避免启动问题提示读到空值。

以上场景是保护依据，本次仅完成实现者命令与 HTTP 自检子集；浏览器、客户端会话和完整沉淀须由审核方独立复验，未建设回归测试。

## 首次沉淀比较同类代码（2026-11-13，Y-D1）

运行目录和仓库的根不同，首次比较须以各自 spec 所在目录给项目辅助文件命名，否则原样复制到 acceptance/guards/ 也会误报全部断言变化。复用旧 spec 库 importRoot 的定位方式；首次比较只保留 spec 与 library.mjs 可达项目依赖，spec 直接 fixtures 导入是可替换的运行适配接口，它引入的随包夹具和运行材料不参与该次断言签名。避免按全局文件名排除：项目 library 导入的同名 scope.mjs 仍须检查。

这项取舍允许守护独立运行时换夹具，不证明替换夹具本身没有吞掉异常或伪造 expect；夹具要保持真实断言与步骤执行，由实际运行及检出能力核对。业务辅助代码放 library。HEAD 基线仍比较完整依赖，不启用首次规范化。

依据：用户返工 2、独立复验 Y/ATS-02-N02 的 verdict 与诊断。开发者用证据副本完成静态比较自检，完整沉淀与独立复验仍待执行。

### 场景标签只标识场景（使用者 2026-11-13 决定）

删除旧版上游用例标签生成命令及其专用测试；保留 tags() 导出，只返回 @<完整场景键>。首次沉淀按场景标签找基线的规则不变。
