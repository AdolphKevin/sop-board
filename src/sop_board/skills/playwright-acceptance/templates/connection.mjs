// 复制到 scenarios/；连接诊断不读取 Cookie、不导航、不改变业务数据。
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';

export function cdpEndpoint(env = process.env) {
  if (env.PW_CDP_ENDPOINT) return env.PW_CDP_ENDPOINT;
  const home = os.homedir();
  const directory = env.PW_CHROME_USER_DATA || (process.platform === 'darwin'
    ? path.join(home, 'Library/Application Support/Google/Chrome')
    : process.platform === 'win32' ? path.join(env.LOCALAPPDATA || path.join(home, 'AppData/Local'), 'Google/Chrome/User Data')
      : path.join(home, '.config/google-chrome'));
  const file = path.join(directory, 'DevToolsActivePort');
  if (!fs.existsSync(file)) throw new Error('未发现 Chrome 调试地址；请核对 chrome://inspect/#remote-debugging 或 PW_CDP_ENDPOINT');
  const [port, wsPath] = fs.readFileSync(file, 'utf8').trim().split(/\r?\n/);
  if (!/^\d+$/.test(port) || !wsPath?.startsWith('/devtools/browser/')) throw new Error('DevToolsActivePort 格式不合格，请刷新 Chrome 调试地址');
  return `ws://127.0.0.1:${port}${wsPath}`;
}

export function connectTimeout(env = process.env) {
  const value = Number(env.PW_CONNECT_TIMEOUT_MS || 60000);
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error('PW_CONNECT_TIMEOUT_MS 应为正整数');
  return value;
}

export async function checkPort(endpoint, timeout = 5000) {
  const url = new URL(endpoint);
  if (!['ws:', 'wss:', 'http:', 'https:'].includes(url.protocol)) throw new Error('CDP 地址须使用 ws/wss/http/https');
  await new Promise((resolve, reject) => {
    const socket = net.connect({host: url.hostname.replace(/^\[|\]$/g, ''), port: Number(url.port || (['wss:', 'https:'].includes(url.protocol) ? 443 : 80))});
    const finish = (error) => { socket.destroy(); error ? reject(error) : resolve(); };
    socket.setTimeout(timeout, () => finish(new Error('Chrome 调试端口连接超时')));
    socket.once('error', () => finish(new Error('Chrome 调试端口不可达；请核对 Chrome 进程、地址及沙箱网络权限')));
    socket.once('connect', () => finish());
  });
}

// 可选目标页模式使用公开 ConnectOverCDPTransport，排除其他休眠页面的初始化。
// 使用 Node >=22 的 WebSocket；不自动关闭标签页，不自动接受调试授权。
export async function targetTransport(endpoint, marker, {timeout = 60000, WebSocketClass = globalThis.WebSocket} = {}) {
  if (!WebSocketClass) throw new Error('PW_CDP_MODE=target 需要 Node >=22；也可用专门的测试 profile 和默认连接模式');
  let websocket = endpoint;
  if (/^https?:/.test(endpoint)) {
    const response = await fetch(endpoint.replace(/\/$/, '') + '/json/version', {signal: AbortSignal.timeout(timeout)});
    if (!response.ok) throw new Error('CDP HTTP 地址未返回 WebSocket 信息；可改用 DevToolsActivePort 中的 ws 地址');
    websocket = (await response.json()).webSocketDebuggerUrl;
  }
  const socket = new WebSocketClass(websocket);
  const sessions = new Set();
  const targets = new Set();
  let commandId = -1;
  let resolveTargets, rejectTargets;
  const transport = {
    send(message) {
      // 不把其他页面暂停在 debugger；目标页仍由 Playwright 正常操作。
      if (message.method === 'Target.setAutoAttach') message = {...message, params: {...message.params, waitForDebuggerOnStart: false}};
      socket.send(JSON.stringify(message));
    },
    close() { try { socket.close(); } catch { /* 连接尚未完成时也保留原错误。 */ } },
  };
  socket.addEventListener('message', ({data}) => {
    const message = JSON.parse(data);
    if (message.id === -1) {
      if (message.error) rejectTargets(new Error('Chrome 未允许读取调试目标'));
      else resolveTargets(message.result.targetInfos);
      return;
    }
    if (message.id < 0) return;
    if (message.method === 'Target.attachedToTarget') {
      const {targetInfo, sessionId} = message.params;
      const allowed = message.sessionId ? sessions.has(message.sessionId)
        : targetInfo.type === 'page' && (targets.has(targetInfo.targetId) || targets.has(targetInfo.openerId));
      if (!allowed) {
        socket.send(JSON.stringify({id: --commandId, method: 'Target.detachFromTarget', params: {sessionId}}));
        return;
      }
      sessions.add(sessionId);
      targets.add(targetInfo.targetId);
    }
    if (message.sessionId && !sessions.has(message.sessionId)) return;
    if (message.method === 'Target.detachedFromTarget' && !sessions.delete(message.params.sessionId)) return;
    transport.onmessage?.(message);
  });
  socket.addEventListener('close', () => {
    rejectTargets?.(new Error('Chrome 调试连接已关闭'));
    transport.onclose?.();
  });
  const opened = new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, {once: true});
    socket.addEventListener('error', () => reject(new Error('Chrome WebSocket 无法连接')), {once: true});
  });
  let timer;
  try {
    const infos = await Promise.race([
      (async () => {
        await opened;
        const result = new Promise((resolve, reject) => {resolveTargets = resolve; rejectTargets = reject;});
        socket.send(JSON.stringify({id: -1, method: 'Target.getTargets'}));
        return result;
      })(),
      new Promise((_, reject) => {timer = setTimeout(() => reject(new Error('端口可达，但 Chrome 未返回调试目标；核对授权提示，不能据此断定用户尚未同意')), timeout);}),
    ]);
    const matches = infos.filter(info => info.type === 'page' && info.url.includes(marker));
    if (matches.length !== 1) throw new Error(`目标标签页匹配 ${matches.length} 个，需要正好一个；请收窄 PW_TAB 或打开目标测试页`);
    targets.add(matches[0].targetId);
    return transport;
  } catch (error) {
    transport.close();
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export async function connectTarget(chromium, env = process.env) {
  const marker = env.PW_TAB;
  if (!marker?.trim()) throw new Error('未设置 PW_TAB（目标标签页 URL 子串）');
  const mode = env.PW_CDP_MODE || 'browser';
  if (!['browser', 'target'].includes(mode)) throw new Error('PW_CDP_MODE 应为 browser 或 target');
  const endpoint = cdpEndpoint(env);
  const timeout = connectTimeout(env);
  await checkPort(endpoint, Math.min(timeout, 5000));
  let browser, transport;
  try {
    transport = mode === 'target' ? await targetTransport(endpoint, marker, {timeout}) : undefined;
    try {
      browser = await chromium.connectOverCDP(transport || endpoint, {timeout, noDefaults: true});
    } catch {
      throw new Error('调试端口可达，但 Playwright 初始化未完成；核对 Chrome 授权提示、浏览器兼容性或休眠标签页。不能仅据超时认定未授权；多标签页可尝试 PW_CDP_MODE=target');
    }
    const pages = browser.contexts().flatMap(context => context.pages()).filter(page => page.url().includes(marker));
    if (pages.length !== 1) throw new Error(`目标标签页匹配 ${pages.length} 个，需要正好一个；核对 PW_TAB`);
    const page = pages[0];
    if (env.PW_FRAME) {
      try { await page.frameLocator(env.PW_FRAME).locator('body').waitFor({state: 'visible', timeout: Math.min(timeout, 15000)}); }
      catch { throw new Error('目标页已连接，但 PW_FRAME 不唯一或 iframe 尚未就绪；核对 surface-map 和实际弹窗，不自动关闭遮挡'); }
    }
    return {browser, page, context: page.context()};
  } catch (error) {
    try { await browser?.close(); } catch { /* 清理错误不能覆盖目标/iframe 诊断。 */ }
    transport?.close();
    throw error;
  }
}
