// 身份池（D95）：每个身份在自己的浏览器（调试端口）里保持登录；切换身份就是换浏览器，不退出、不在同一浏览器里换账号。
// 读取任务目录 identities.json（PW_IDENTITIES）。真实登录或请求验证码前，先运行
//   sop-board identity record --entry <入口> --identity <身份> --event login|code --task <任务编号>
// 被拒时停下，把该身份的场景记为受阻，事项已登记到待人处理。
import fs from 'node:fs';
import {chromium} from '@playwright/test';

const connections = new Map();

export function identity(id, entry = process.env.PW_ATLAS_ENTRY) {
  const file = process.env.PW_IDENTITIES;
  if (!file || !fs.existsSync(file)) throw new Error('未提供 identities.json（PW_IDENTITIES）');
  const pools = JSON.parse(fs.readFileSync(file, 'utf8')).entries || {};
  const names = entry ? [entry] : Object.keys(pools);
  for (const name of names) {
    const found = (pools[name] || []).find(item => item.id === id || item.label === id);
    if (found) return {...found, entry: name};
  }
  throw new Error(`没有声明身份 ${id}`);
}

// 返回该身份浏览器中的第一个页面（已登录状态由项目钩子或人维护）；同一身份只连接一次。
export async function as(id, {entry} = {}) {
  const spec = identity(id, entry);
  if (!spec.cdp) throw new Error(`身份 ${spec.id} 没有声明浏览器调试地址 cdp`);
  let connection = connections.get(spec.id);
  if (!connection || !connection.browser.isConnected()) {
    const browser = await chromium.connectOverCDP(spec.cdp);
    connection = {browser, context: browser.contexts()[0] || await browser.newContext()};
    connections.set(spec.id, connection);
  }
  return connection.context.pages()[0] || connection.context.newPage();
}

// 本轮结束时断开身份浏览器的调试连接（不关闭浏览器，登录状态保留）。
export async function release() {
  for (const {browser} of connections.values()) {
    try { await browser.close(); } catch { /* 连接已断开 */ }
  }
  connections.clear();
}
