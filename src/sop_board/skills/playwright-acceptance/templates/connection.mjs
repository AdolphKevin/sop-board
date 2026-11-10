// 复制到 scenarios/。只连接看板守护进程启动的 Chrome for Testing（run.env 的 PW_CDP_ENDPOINT）；
// 不启动浏览器、不连接用户日常 Chrome。连接诊断不读取 Cookie、不导航、不改变业务数据。
import net from 'node:net';

export function cdpEndpoint(env = process.env) {
  if (env.PW_CDP_ENDPOINT?.trim()) return env.PW_CDP_ENDPOINT.trim();
  throw new Error('未设置 PW_CDP_ENDPOINT：先在运行目录 `set -a; . ./run.env; set +a`；浏览器状态见 sop-board browser status');
}

export function connectTimeout(env = process.env) {
  const value = Number(env.PW_CONNECT_TIMEOUT_MS || 30000);
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error('PW_CONNECT_TIMEOUT_MS 应为正整数');
  return value;
}

export async function checkPort(endpoint, timeout = 5000) {
  const url = new URL(endpoint);
  if (!['ws:', 'http:'].includes(url.protocol)) throw new Error('CDP 地址须使用 http 或 ws');
  await new Promise((resolve, reject) => {
    const socket = net.connect({host: url.hostname.replace(/^\[|\]$/g, ''), port: Number(url.port || 80)});
    const finish = (error) => { socket.destroy(); error ? reject(error) : resolve(); };
    socket.setTimeout(timeout, () => finish(new Error('浏览器调试端口连接超时')));
    socket.once('error', () => finish(new Error('浏览器调试端口不可达；用 sop-board browser status 查看浏览器是否在运行或已崩溃')));
    socket.once('connect', () => finish());
  });
}

// 连接看板的浏览器，使用其持久 profile 的默认上下文（保留登录态）。
// PW_TAB 可选：只有一个标签页的 URL 含该子串时复用它；没有匹配则新开一页；匹配多于一个时报错。
export async function connectBroker(chromium, env = process.env) {
  const endpoint = cdpEndpoint(env);
  const timeout = connectTimeout(env);
  await checkPort(endpoint, Math.min(timeout, 5000));
  let browser;
  try {
    browser = await chromium.connectOverCDP(endpoint, {timeout});
    const context = browser.contexts()[0] || await browser.newContext();
    const marker = env.PW_TAB?.trim();
    const matches = marker ? context.pages().filter(page => page.url().includes(marker)) : [];
    if (matches.length > 1) throw new Error(`目标标签页匹配 ${matches.length} 个，需要至多一个；收窄 PW_TAB`);
    const page = matches[0] || await context.newPage();
    if (env.PW_FRAME) {
      try { await page.frameLocator(env.PW_FRAME).locator('body').waitFor({state: 'visible', timeout: Math.min(timeout, 15000)}); }
      catch { throw new Error('PW_FRAME 不唯一或 iframe 尚未就绪；核对 surface-map 和实际页面'); }
    }
    return {browser, page, context};
  } catch (error) {
    try { await browser?.close(); } catch { /* 清理错误不能覆盖原诊断。 */ }
    throw error;
  }
}
