import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import {EventEmitter} from 'node:events';
import {createHash} from 'node:crypto';
import {cdpEndpoint, connectTarget, targetTransport} from '../templates/connection.mjs';
import {readScope, scopeAnnotation} from '../templates/scope.mjs';
import {recording} from '../templates/recording.mjs';

async function portTest(action) {
  const server = net.createServer(socket => socket.end());
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try { await action(`ws://127.0.0.1:${server.address().port}/devtools/browser/test`); }
  finally { await new Promise(resolve => server.close(resolve)); }
}

test('endpoint validates the actual discovery file', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'pw-endpoint-'));
  try {
    fs.writeFileSync(path.join(directory, 'DevToolsActivePort'), '9222\n/devtools/browser/test\n');
    assert.equal(cdpEndpoint({PW_CHROME_USER_DATA: directory}), 'ws://127.0.0.1:9222/devtools/browser/test');
    fs.writeFileSync(path.join(directory, 'DevToolsActivePort'), 'invalid');
    assert.throws(() => cdpEndpoint({PW_CHROME_USER_DATA: directory}), /格式/);
  } finally { fs.rmSync(directory, {recursive: true}); }
});

test('wrong target count disconnects and avoids leaking query strings', async () => {
  await portTest(async endpoint => {
    let closed = 0;
    const chromium = {connectOverCDP: async (_, options) => {
      assert.equal(options.noDefaults, true);
      return {contexts: () => [{pages: () => []}], close: async () => closed++};
    }};
    await assert.rejects(connectTarget(chromium, {PW_TAB: '/app?secret=hidden', PW_CDP_ENDPOINT: endpoint}), error => {
      assert.match(error.message, /匹配 0/);
      assert.doesNotMatch(error.message, /secret/);
      return true;
    });
    assert.equal(closed, 1);
  });
});

test('CDP initialization timeout is not labelled user denial', async () => {
  await portTest(async endpoint => {
    await assert.rejects(connectTarget({connectOverCDP: async () => {throw Error('timeout');}}, {PW_TAB: '/app', PW_CDP_ENDPOINT: endpoint}), /不能仅据超时认定未授权/);
  });
});

test('disconnect failure does not replace the original selection diagnosis', async () => {
  await portTest(async endpoint => {
    const browser = {contexts: () => [{pages: () => []}], close: async () => {throw Error('cleanup failed');}};
    await assert.rejects(connectTarget({connectOverCDP: async () => browser}, {PW_TAB: '/app', PW_CDP_ENDPOINT: endpoint}), /匹配 0/);
  });
});

test('iframe preflight waits on the declared frame and disconnects on failure', async () => {
  await portTest(async endpoint => {
    let closed = 0, selected;
    const page = {url: () => 'https://test/app', frameLocator: selector => {
      selected = selector;
      return {locator: () => ({waitFor: async () => {throw Error('not ready');}})};
    }};
    const browser = {contexts: () => [{pages: () => [page]}], close: async () => closed++};
    await assert.rejects(connectTarget({connectOverCDP: async () => browser}, {PW_TAB: '/app', PW_CDP_ENDPOINT: endpoint, PW_FRAME: 'iframe[name="modal"]'}), /iframe/);
    assert.equal(selected, 'iframe[name="modal"]');
    assert.equal(closed, 1);
  });
});

function fakeSocketClass(infos) {
  return class FakeSocket extends EventTarget {
    static instances = [];
    constructor() { super(); this.sent = []; this.closed = false; this.constructor.instances.push(this); queueMicrotask(() => this.dispatchEvent(new Event('open'))); }
    send(raw) {
      const message = JSON.parse(raw); this.sent.push(message);
      if (message.id === -1 && infos) queueMicrotask(() => this.emit({id: -1, result: {targetInfos: infos}}));
    }
    emit(message) { this.dispatchEvent(Object.assign(new Event('message'), {data: JSON.stringify(message)})); }
    close() { if (!this.closed) {this.closed = true; this.dispatchEvent(new Event('close'));} }
  };
}

test('target transport keeps only selected page, child frames and its popups', async () => {
  const Socket = fakeSocketClass([{type: 'page', targetId: 'main', url: 'https://test/app'}, {type: 'page', targetId: 'sleeping', url: 'https://other/'}]);
  const transport = await targetTransport('ws://localhost/test', '/app', {WebSocketClass: Socket});
  const socket = Socket.instances[0], received = [];
  transport.onmessage = message => received.push(message);
  const attached = (targetId, sessionId, parent, openerId, type = 'page') => socket.emit({method: 'Target.attachedToTarget', ...(parent ? {sessionId: parent} : {}), params: {sessionId, targetInfo: {targetId, type, openerId}}});
  attached('sleeping', 'unrelated');
  socket.emit({method: 'Runtime.consoleAPICalled', sessionId: 'unrelated'});
  attached('main', 'main-session');
  attached('child', 'frame-session', 'main-session', undefined, 'iframe');
  attached('popup', 'popup-session', undefined, 'main');
  assert.equal(received.length, 3);
  assert.equal(socket.sent.filter(m => m.method === 'Target.detachFromTarget').length, 1);
  transport.send({id: 1, method: 'Target.setAutoAttach', params: {autoAttach: true, waitForDebuggerOnStart: true}});
  assert.equal(socket.sent.at(-1).params.waitForDebuggerOnStart, false);
  transport.close();
  assert.equal(socket.closed, true);
});

test('target transport rejects ambiguous selection and releases the socket', async () => {
  const Socket = fakeSocketClass([{type: 'page', targetId: '1', url: '/app'}, {type: 'page', targetId: '2', url: '/app'}]);
  await assert.rejects(targetTransport('ws://localhost/test', '/app', {WebSocketClass: Socket}), /匹配 2/);
  assert.equal(Socket.instances[0].closed, true);
});

test('target transport times out without inferring denial or leaving a socket', async () => {
  const Socket = fakeSocketClass(null);
  await assert.rejects(targetTransport('ws://localhost/test', '/app', {WebSocketClass: Socket, timeout: 10}), /不能据此断定/);
  assert.equal(Socket.instances[0].closed, true);
});

test('scope detects changed source and emits a batch-bound report annotation', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'pw-scope-'));
  try {
    const source = path.join(directory, 'scenarios.yaml'), scopeFile = path.join(directory, 'scope.json');
    fs.writeFileSync(source, 'original');
    const scope = {project: 'repo', batch: 'batch', scenario_file_sha256: createHash('sha256').update('original').digest('hex'), scenarios: {'SOP-1': 'b'.repeat(64)}};
    fs.writeFileSync(scopeFile, JSON.stringify(scope));
    const env = {PW_SCOPE: scopeFile, PW_SCENARIO_FILE: source};
    const read = readScope(env), annotation = scopeAnnotation(read, 'SOP-1');
    assert.equal(JSON.parse(annotation.description).fingerprint, scope.scenarios['SOP-1']);
    assert.throws(() => scopeAnnotation(read, 'OTHER-1'), /不在本轮/);
    fs.writeFileSync(source, 'changed');
    assert.throws(() => readScope(env), /SHA-256/);
  } finally { fs.rmSync(directory, {recursive: true}); }
});

async function captureTest(failure, action) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'pw-recording-'));
  const calls = [], attached = [];
  const context = new EventEmitter();
  context.tracing = {start: async () => {calls.push('trace-start'); if (failure === 'trace-start') throw Error('start failed');}, stop: async ({path: target}) => {calls.push('trace-stop'); if (failure === 'trace-stop') throw Error('stop failed'); fs.writeFileSync(target, 'trace');}};
  const page = {evaluate: async () => ({width: 800, height: 600}), screencast: {
    start: async ({path: target}) => {calls.push('video-start'); fs.writeFileSync(target, 'video'); if (failure === 'video-start') throw Error('partial start');},
    showActions: async () => {}, hideActions: async () => calls.push('hide-actions'), stop: async () => calls.push('video-stop'),
  }};
  const info = {title: 'SOP-1', outputPath: name => path.join(directory, name), attach: async (name, payload) => attached.push({name, payload})};
  try { await action({capture: recording(page, context, info), calls, attached, context, page}); }
  finally { fs.rmSync(directory, {recursive: true}); }
}

test('recording releases partial initialization and saves available evidence', async () => {
  for (const stage of ['video-start', 'trace-start']) {
    await captureTest(stage, async ({capture, calls, attached, context}) => {
      await assert.rejects(capture.start());
      await capture.stop();
      assert.ok(calls.includes('video-stop'));
      assert.equal(context.listenerCount('page'), 0);
      assert.ok(attached.some(a => a.name === 'video'));
    });
  }
});

test('trace stop failure does not prevent video cleanup or attachment', async () => {
  await captureTest('trace-stop', async ({capture, calls, attached, context}) => {
    await capture.start();
    const errors = await capture.stop();
    assert.ok(errors.includes('trace 保存失败'));
    assert.ok(calls.includes('video-stop'));
    assert.ok(attached.some(a => a.name === 'video'));
    assert.equal(context.listenerCount('page'), 0);
  });
});

test('unrelated new tab is not recorded', async () => {
  await captureTest(null, async ({capture, context}) => {
    await capture.start();
    context.emit('page', {opener: async () => null});
    assert.deepEqual(await capture.stop(), []);
  });
});
