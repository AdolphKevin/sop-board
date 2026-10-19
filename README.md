# sop-board

SOP 验证协作看板。开发岗的代理用 [praxis-skills](https://github.com/AdolphKevin/praxis-skills) 的 `sop-verify` 技能，把业务 SOP 的验收场景与每轮验收结果推送到看板；测试岗在同一个网页上传测试用例、查看进度、复核 Agent 判为通过的场景，并按场景提交缺陷。看板展示 Agent 验收通过率、人工复测通过率与测试进度。

状态：开发中。

## 使用方式

任何成员 clone 本仓后用 uv 在本机启动看板；团队共用一份数据时，由启动者开启内网访问，其他成员用浏览器打开。分析测试用例时，看板调用启动者本机已登录的 Codex。暂不提供服务器部署。

## 相关仓库

- [praxis-skills](https://github.com/AdolphKevin/praxis-skills)：`sop-verify` 技能，从业务 SOP 写场景、派独立会话执行、登记结果。
- [team-workflow](https://github.com/AdolphKevin/team-workflow)：团队流程；看板按轮次导出的结果可直接用 `tw team scenarios submit` 登记。

## 启动与内网访问

需要 Python 3.10 或以上版本与 uv。在仓库目录安装依赖：

```sh
uv sync
```

在终端将自行选择的写入令牌设置到 `SOP_BOARD_TOKEN` 环境变量，再启动服务（令牌不要写入仓库）：

```sh
uv run sop-board --data ./board-data --port 8787
```

`--data <目录>` 指定数据目录，不存在时自动创建。看到 `看板地址：http://127.0.0.1:8787/` 后即可访问首页，选择项目与批次，查看队列、指标、场景历史及证据，并按轮次导出 ZIP。

默认只允许本机连接。需要内网共用时，监听本机的全部网络接口：

```sh
uv run sop-board --data ./board-data --host 0.0.0.0 --port 8787
```

启动输出中的 `看板地址：http://127.0.0.1:8787/` 供启动者在本机打开；每个可用的内网 IPv4 地址会另起一行，例如 `内网地址：http://192.168.1.20:8787/`。将对应网络的这条内网地址发给其他成员访问；`0.0.0.0` 表示监听全部网络接口，不是浏览器访问地址。所有能连接该地址的人都能读取场景、证据和导出；写入接口依靠 `SOP_BOARD_TOKEN` 校验权限。服务使用 HTTP，没有传输加密，因此只应在可信内网使用，不要把端口直接暴露到公网。

在批次导航点“SOP 场景”，可按节点浏览全部场景，并筛选全部、未人工复核、Agent 已通过、不通过或受阻。筛选地址带 `?filter=`，可复制分享。场景列表与详情页共用“通过”“失败”“受阻”三个按钮：通过立即提交；失败与受阻只需填写缺陷链接，备注选填，也可取消。保存后在原处更新结果与数量，人工结果统一显示“通过”“不通过（失败）”或“不通过（受阻）”。接口拒绝时在该场景下显示错误代码和各项问题说明，并保留已填内容。

首次复核时在页面顶部的身份栏填写标记人与写入令牌，二者分别保存在当前浏览器的 `sop-board.marker`、`sop-board.token` 本地存储中，可随时更换。共享电脑使用后应清除该站点的本地存储。接口写入请求使用 `Authorization: Bearer <令牌>`。

人工标记接口只接受 `scenario`、`fingerprint`、`status`、`marker`、`defect_url`、`note`。状态只支持 `pass`、`fail`、`blocked`；通过不带缺陷链接，失败与受阻必须带链接。实际结果、问题记录与观察证据由看板生成，不再接收人工填写的 `actual`、`environment`、`finding`、`evidence`。观察记录使用 `marker`、`status`、`recorded_at`，有值时再写入 `defect_url` 与 `note`。按轮导出的 ZIP 保留原有 tw 结果格式，观察文件位于 `evidence/<序号>-observation-<轮次>.json`。

缺陷链接去掉首尾空白后核对。启动前设置可选的 `JIRA_URL`（如 `https://example.atlassian.net`），即可填写 `ABC-1` 这样的 Jira 单号，或该站点的 `/browse/ABC-1` 完整链接；看板会去掉 Jira 链接中的查询参数与片段。也可填写符合项目缺陷链接前缀的完整链接，比较不区分大小写，内容保持原样。GitHub 项目默认前缀为该仓库的 Issues 地址；其他项目可通过项目设置接口登记 HTTPS 前缀。未配置 `JIRA_URL` 时，单号须改填完整链接；两种配置都没有时须先设置其中一种。已登记的旧链接保持原样。

按 Ctrl+C 或发送终止信号停止服务；下次用同一数据目录启动，数据会保留。

## 运行测试

```sh
uv run --frozen pytest -q -p no:cacheprovider tests
```

表单脚本的隔离逻辑测试还需要本机安装 Node.js 18 或以上版本，不需要安装 npm 包。其余测试通过临时目录中的真实 HTTP 服务与 SQLite 核对结果；浏览器操作和内网另一台机器的访问仍需另行验收。
