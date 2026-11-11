# Web 场景验收

Web 场景由执行会话在运行目录里用随包技能执行。技能、模板与转换器维护在 `src/sop_board/skills/playwright-acceptance/`，随 sop-board 包发行，执行者读取的操作说明见[技能正文](../src/sop_board/skills/playwright-acceptance/SKILL.md)。业务验收标准见[统一标准](standard.md)，项目操作、账号与清理见产品仓库的真实验证手册。

## 安装运行时

```sh
uv tool install /path/to/sop-board
sop-board skills install        # 把 sop-board、regression-distill、playwright-acceptance 链接到两个客户端
sop-board browser install       # 安装固定版本的 Playwright 与 Chrome for Testing
sop-board playwright guide      # 只看 Web 操作
```

运行时装在 `~/.sop-board/runtime`（可用 `SOP_BOARD_HOME` 改位置），自带 ffmpeg，不使用 `~/Library/Caches/ms-playwright` 之类的共享缓存。执行者不运行 `playwright install`。

## 一次 Web 运行

1. 看板守护进程（`sop-board serve`）持有浏览器：为每个 检出×身份 启动一个 Google Chrome for Testing，默认无头（`--headless=new`），一份持久 profile 保存登录态。用户日常 Chrome 不受影响。
2. `sop-board run prepare` 租用所需身份的浏览器，写出运行目录：`prompt.md`、`brief.md`、`scenarios.yaml`、`pending.json`、`entries.json`、`scope.json`、`identities.json`、`run.env`、`evidence/`，以及指向看板运行时的 `node_modules`。`run.env` 含 `PW_CDP_ENDPOINT`、`PW_IDENTITIES`、`PW_SCOPE`、`PW_SCENARIO_FILE`、`PW_OUT` 等。
3. 执行会话把技能的模板复制到运行目录，经 `PW_CDP_ENDPOINT` 连接浏览器，用 `workers: 1`、`retries: 0`、`maxFailures: 1` 执行。用例标题以场景 key 开头（`shop#W 标题`）。
4. 报告转换并登记：

   ```sh
   sop-board playwright convert --report playwright/report.json --out . --executor <会话> \
     --scope scope.json --scenario-file scenarios.yaml [--allow-partial]
   sop-board run submit --run <N>
   ```

   证据复制到 `evidence/<场景 key，# 换成 __>/`。`--allow-partial` 用于中途退出，未执行的场景写成 `not-executed` 并留在待执行范围。结果只经 `run submit` 登记一次。

录屏与 trace 的开关按场景固定在 `scope.json`，取自入口的 `web_evidence`；关闭的项不启动、不要求附件，本轮全部关闭录屏时不检查 ffmpeg。缺省时间上限为单场景 4 分钟、定位或操作 15 秒、导航 30 秒。

## 真实账号登录

项目声明的测试账号由执行者直接用声明的凭据变量登录。真实账号、未声明的账号、人机验证与只发到用户本人设备的验证码由人在看板打开的窗口完成，完成后交还，窗口保持打开：

```sh
sop-board browser open --identity <身份> --headed   # 有界面的 CfT 窗口，人登录或完成验证
sop-board browser release --identity <身份>          # 交还：窗口不关，之后的运行直接沿用
```

人持有窗口期间，`run prepare` 对该身份返回 `browser-busy`。看到 "browser may not be secure" 时停止，执行者把场景记为 blocked。每次真实登录或请求验证码前，执行者先运行 `sop-board identity record --identity <身份> --kind login|code --run <N>`；超限被拒绝并登记到待人处理。

## 崩溃

- 浏览器进程在运行中退出：`run submit` 把该身份相关且未通过的 Web 场景记为 blocked，finding 写明信号与 `chrome.log` 路径，并登记待人处理；已写出的 pass 保留。
- 渲染进程崩溃（`Target crashed`）：夹具按 blocked 处理并停止本轮。
- 租约持有期间不自动重启。30 分钟内异常退出两次，该身份进入 `browser-crashed`，须检查后运行 `sop-board browser open --identity <身份> --reset-crash`。
- `sop-board browser status` 查看各身份浏览器状态。

## 浏览器层 spec 保真

产品仓库里的浏览器层守护 spec 由 regression-distill 沉淀，索引是场景目录下的 `regression.yaml`（`sop-board distill submit` 写入）。改写或撤销其断言时，先运行：

```sh
sop-board specs check --repo <仓库别名> <路径> [--ledger ledger.json]
```

它以 git HEAD 为基线比较断言。改写须在账本写 `{assertion_id, kind: "rewrite", reason}`，撤销须写 `{kind: "withdraw", reason, ref}`，`ref` 指向业务预期变更的确认来源；缺账本的变化为 violation 并以非零退出。细则见[spec 保真](../src/sop_board/skills/playwright-acceptance/references/spec-fidelity.md)。

## 验证与维护

- `sop-board playwright` 的子命令是 `guide`、`convert`、`tags`；`tags` 把上游配对文件转成用例 tag，不表示通过。
- 验收以 `docs/sop/` 场景经真实入口执行为准，不新增单元、接口或集成测试。由人触发的回归沉淀用 [regression-distill](../src/sop_board/skills/regression-distill/SKILL.md)。
- 仅更新技能说明与模板不会改写已准备好的运行目录里的脚本；继续旧运行目录前核对其脚本与完成等待配置。
