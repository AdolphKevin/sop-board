// 在一个进程里依次执行一次 browser do 的全部驱动命令（已定决策 31 第二段）。
// 用法：node driver-batch.cjs <playwright-core 目录>，标准输入为
//   {"session": 身份, "steps": [[子命令, 参数…]…], "after": [[子命令, 参数…]…], "timeout_ms": 每步上限}
// steps 遇到失败即停；after（取页面结构、截图）总会执行。每条命令输出一行 JSON：{phase, index, ok, text, ms}。
// 参数解析与发送方式照 playwright-cli 自己的客户端（同一版本的内部模块），所以与逐条运行 playwright-cli 等价。
'use strict';
const path = require('path');
const core = process.argv[2];
const lib = name => require(path.join(core, 'lib', 'tools', 'cli-client', name));
const { minimist } = lib('minimist.js');
const { Registry, createClientInfo } = lib('registry.js');
const { Session } = lib('session.js');
const help = require(path.join(core, 'lib', 'tools', 'cli-client', 'help.json'));

function parse(argv) {
  const args = minimist(argv, { boolean: [...help.booleanOptions, 'all', 'g', 'help', 'json', 'raw', 'version'], string: ['_'] });
  for (const name of ['s', 'session', 'g', 'global', 'json', 'raw', 'help', 'version'])
    delete args[name];
  return args;
}

async function main() {
  let raw = '';
  for await (const chunk of process.stdin)
    raw += chunk;
  const request = JSON.parse(raw);
  const clientInfo = createClientInfo();
  const registry = await Registry.load();
  const entry = registry.entry(clientInfo, request.session);
  if (!entry) {
    process.stdout.write(JSON.stringify({ phase: 'session', ok: false, text: `The browser '${request.session}' is not open` }) + '\n');
    return;
  }
  const session = new Session(entry);
  const run = async (phase, index, argv) => {
    const started = Date.now();
    let timer;
    const limit = new Promise(resolve => { timer = setTimeout(() => resolve({ timedOut: true }), request.timeout_ms); });
    let result;
    try {
      result = await Promise.race([session.run(clientInfo, parse(argv), { raw: false, json: false }), limit]);
    } catch (error) {
      result = { isError: true, text: '### Error\n' + String(error && error.message || error) };
    }
    clearTimeout(timer);
    const line = result.timedOut
      ? { phase, index, ok: false, timed_out: true, text: '', ms: Date.now() - started }
      : { phase, index, ok: !result.isError, text: result.text || '', ms: Date.now() - started };
    process.stdout.write(JSON.stringify(line) + '\n');
    return line.ok;
  };
  let failed = false;
  for (let index = 0; index < request.steps.length && !failed; index++)
    failed = !(await run('step', index, request.steps[index]));
  for (let index = 0; index < (request.after || []).length; index++)
    await run('after', index, request.after[index]);
}

main().then(() => process.exit(0), error => {
  process.stdout.write(JSON.stringify({ phase: 'batch', ok: false, text: String(error && error.stack || error) }) + '\n');
  process.exit(1);
});
