// 验证真实 Playwright fixture 调度；不连接 Chrome，也不操作业务数据。
// PW_TEST_PACKAGE 可指向已安装的 @playwright/test 目录。
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

test('four tests share one CDP connection but save four independent recordings', () => {
  const require = createRequire(import.meta.url);
  const packageDir = process.env.PW_TEST_PACKAGE || path.dirname(require.resolve('@playwright/test/package.json'));
  const pkg = JSON.parse(fs.readFileSync(path.join(packageDir, 'package.json'), 'utf8'));
  const cli = path.join(packageDir, typeof pkg.bin === 'string' ? pkg.bin : pkg.bin.playwright);
  const templates = fileURLToPath(new URL('../templates/', import.meta.url));
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pw-worker-reuse-'));
  try {
    fs.mkdirSync(path.join(dir, 'scenarios'));
    for (const name of ['playwright.config.mjs', 'global-setup.mjs', 'progress-reporter.mjs']) fs.copyFileSync(path.join(templates, name), path.join(dir, name));
    for (const name of ['fixtures.mjs', 'scope.mjs', 'recording.mjs']) fs.copyFileSync(path.join(templates, name), path.join(dir, 'scenarios', name));
    fs.mkdirSync(path.join(dir, 'node_modules', '@playwright'), {recursive: true});
    fs.symlinkSync(packageDir, path.join(dir, 'node_modules', '@playwright', 'test'), 'dir');
    const log = path.join(dir, 'calls.jsonl');
    fs.writeFileSync(path.join(dir, 'scenarios', 'connection.mjs'), `
import fs from 'node:fs';
import {EventEmitter} from 'node:events';
const log = value => fs.appendFileSync(${JSON.stringify(log)}, JSON.stringify(value)+'\\n');
export const cdpEndpoint = () => 'ws://test';
export const connectTimeout = () => 60000;
export const checkPort = async () => log('port-preflight');
export async function connectTarget() {
 log('connect');
 const context = new EventEmitter();
 context.tracing = {start: async () => log('trace-start'), stop: async ({path}) => {log('trace-stop');fs.writeFileSync(path,'trace');}};
 let video;
 const page = {isClosed:()=>false, evaluate:async()=>({width:800,height:600}), value:'test-page',
   screencast:{start:async({path})=>{video=path;log('video-start');},showActions:async()=>{},hideActions:async()=>{},stop:async()=>{log('video-stop');fs.writeFileSync(video,'video');}}};
 context.setDefaultTimeout=()=>{}; context.setDefaultNavigationTimeout=()=>{}; context.pages=()=>[page];
 const browser = {isConnected:()=>true,close:async()=>log('disconnect')};
 return {browser,page,context};
}
`);
    fs.writeFileSync(path.join(dir, 'scenarios', 'reuse.spec.mjs'), `
import {test,expect} from './fixtures.mjs';
for(let i=1;i<=4;i++)test('SOP-REUSE-P0'+i,async({page})=>expect(page.value).toBe('test-page'));
`);
    const env = {...process.env, PW_TAB: '/test', PW_OUT: path.join(dir, 'out')};
    // 本测试验证连接生命周期，不继承调用者的业务范围快照。
    delete env.PW_SCOPE; delete env.PW_SCENARIO_FILE;
    execFileSync(process.execPath, [cli, 'test', '--config', path.join(dir, 'playwright.config.mjs')], {cwd: dir, env, timeout: 60000, stdio: 'pipe'});
    const calls = fs.readFileSync(log, 'utf8').trim().split('\n').map(JSON.parse);
    const count = name => calls.filter(value => value === name).length;
    assert.equal(count('port-preflight'), 1);
    assert.equal(count('connect'), 1);
    assert.equal(count('disconnect'), 1);
    for (const name of ['trace-start', 'trace-stop', 'video-start', 'video-stop']) assert.equal(count(name), 4);
    const report = JSON.parse(fs.readFileSync(path.join(dir, 'out', 'report.json'), 'utf8'));
    assert.equal(report.stats.expected, 4);
    assert.equal(report.stats.unexpected, 0);
  } finally { fs.rmSync(dir, {recursive: true, force: true}); }
});
