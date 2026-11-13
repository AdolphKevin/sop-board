# 多人共享验收记录

配了 `shared_database` 的项目，结果、标注、守护运行、沉淀登记、修复、收件箱、问答、批注与同意都在团队的共享库里，按**产品**归属；证据文件留在产出它的机器上。是不是同一个产品由使用者决定。

## 接入共享库

先用 `sop-board status --json` 判断：`product.shared` 为 false 表示还没有连接；`product-unbound` 表示已写连接、尚未绑定产品。配置不完整、status 尚无 product 时，先按主技能启动看板，从回环地址的 `/api/settings` 读取无密码状态，用 sections.shared.content 是否有 shared_database 判断。会话不读取、不输出 config.yaml 的 `shared_database.password`。接入全程不读取仓库 origin，不查看目录或别的仓库；同一个产品由使用者选择。

### 还没有连接

1. 按主技能的“守护进程”步骤确认看板在运行。从 status 的 `daemon.url` 取得回环地址，给使用者完整的 `<daemon.url>/p/<项目>/settings` 链接（项目名按 URL 编码；不要给内网地址）。
2. 请使用者在设置页“共享库”填写连接与密码、测试连接，并选择加入或新建产品。会话不引导手写配置或密码文件，不索要、读取或代填密码，不自行写入连接或绑定产品。
3. 使用者说已接入后，再运行 `sop-board status --json` 核对产品名称与共享状态，转告结果；仍未接入就报告实际状态。

### 已有连接、尚未绑定，或明确要求换产品

1. `sop-board init <项目> --json` 输出 `product-choice-required` 和 `products`。把名称、创建人、有结果的场景数、最近写入时间与 `hint` 告诉使用者，问加入哪个产品，还是新建（名称缺省为项目名）。等明确答复。
2. 按答复运行 `sop-board init <项目> --join <ID>` 或 `--new [--product <名称>]`，再 `sop-board status --json` 确认。
3. 转告绑定输出的 `notes`：本机已有结果不带入共享库、已登记守护须在共享库重新验收后再沉淀；`unregistered_aliases` 所列别名本机未登记，引用它们的守护在本机无法运行。

### 连接错误

`database-authentication`：给出上述本机设置页链接，请使用者核对连接并改密码；使用者处理后用 status 重试。`database-unreachable` 与 `database-version`：照错误输出报告连接位置或两边版本，等使用者处理，沿用原有规则。

## 跨机器验收

在这台机器上验收别人实现的功能时，实现者是别的操作者：`sop-board run submit --run <编号> --implemented-by <实现者的操作者>`（`run launch` 同名参数），记为独立验收。实现者由使用者在任务里说明；使用者说的是本机会话实现的，改用 `--implementer <会话编号>`；没说明就两个都不填，报告为独立性未声明。

## 状态怎么读

- 结论（Agent 的通过与失败、人工标注、修复关闭）各机器共用，按分支归属。本分支上没有结论时用其他分支的，`status --json` 的 `from_other_branch` 与看板的“来源分支”标出来；报告时照实说明这些场景的结论来自哪个分支。
- 受阻、跳过与守护未通过只影响产出它的机器和检出。别的机器上的，看板场景详情的“其他机器上的情况”里能看到，本机状态不变。
- `distilled_elsewhere` 里的场景已由别人沉淀、本机还没同步那条守护：告诉使用者拉取对应的提交后守护才在本机生效；在那之前 `retest` 不运行它。
- `retest` 输出的 `cannot_run`（本机无法运行）是守护引用的仓库本机没有登记，转告使用者；不是代码回归，也不为此登记仓库或改配置。

## 别的机器产出的修复

`status --json` 的 `fixes` 列出产品内进行中的修复，带产出机器与检出。`fix submit`、`run prepare --fix`、`fix close`、`fix abandon` 只在产出修复的机器上执行，别处返回 `fix-not-local`：报告修复在哪台机器、哪个检出。使用者要在本机验证时，等修复提交推送、本机拉取之后用 `sop-board run prepare --scope <场景>` 按 [run.md](run.md) 执行；结果是共享结论，修复仍由产出机器关闭。

## 证据在别的机器上

看板与报告从产出机器的看板（需以 `--lan` 运行）读取证据。读不到时显示“证据在 <机器> 的机器上”，`report --zip` 的 `missing_evidence` 是缺少的条数：照实报告，请那台机器的使用者以 `--lan` 启动看板后再导出。
