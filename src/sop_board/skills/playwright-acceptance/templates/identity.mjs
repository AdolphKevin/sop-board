// 身份：每个身份是看板为本检出启动的一个 Chrome for Testing（独立持久 profile），切换身份就是换浏览器。
// 读取运行目录 identities.json（PW_IDENTITIES），其中每个身份带 endpoint。真实登录或请求验证码前先运行
//   sop-board identity record --identity <身份> --kind login|code --run <运行编号>
// 被拒时停下，把该身份的场景记为受阻（已登记到待人处理）。声明的测试账号由执行者用凭据变量登录；真实账号由人在
// sop-board browser open --headed 的窗口中登录后 browser release 交还，窗口保持打开，这里直接沿用。
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

// 在该身份的浏览器里新开一页（登录态在 profile 中）；同一身份只连接一次。
export async function as(id, {entry} = {}) {
  const spec = identity(id, entry);
  if (!spec.endpoint) throw new Error(`身份 ${spec.id} 的浏览器没有可用地址（可能启动失败或已崩溃，见 brief.md）`);
  let connection = connections.get(spec.id);
  if (!connection || !connection.browser.isConnected()) {
    const browser = await chromium.connectOverCDP(spec.endpoint);
    connection = {browser, context: browser.contexts()[0] || await browser.newContext()};
    connections.set(spec.id, connection);
  }
  return connection.context.newPage();
}

// 本轮结束时断开身份浏览器的调试连接（不关闭浏览器，登录状态保留）。
export async function release() {
  for (const {browser} of connections.values()) {
    try { await browser.close(); } catch { /* 连接已断开 */ }
  }
  connections.clear();
}
