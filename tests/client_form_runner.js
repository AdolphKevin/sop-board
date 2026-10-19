// Isolated DOM boundary; selected cases also call the disposable local HTTP service.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));

class Element {
  constructor(tag, attrs = {}, children = []) {
    this.tagName = tag.toUpperCase(); this.attrs = {...attrs}; this.handlers = {};
    this.children = []; this.parentElement = null; this.disabled = false; this.focused = false;
    this.value = attrs.value || "";
    this.dataset = new Proxy({}, {
      get: (_, key) => this.attrs["data-" + key.replace(/[A-Z]/g, c => "-" + c.toLowerCase())],
      set: (_, key, value) => {this.attrs["data-" + key.replace(/[A-Z]/g, c => "-" + c.toLowerCase())] = String(value); return true;},
    });
    for (const child of children) this.appendChild(child);
    if (tag === "textarea") this.value = this.textContent;
  }
  get textContent() { return this.children.map(c => typeof c === "string" ? c : c.textContent).join(""); }
  set textContent(value) { this.replaceChildren(String(value)); }
  get hidden() { return "hidden" in this.attrs; }
  set hidden(value) { if (value) this.attrs.hidden = ""; else delete this.attrs.hidden; }
  get action() { return this.attrs.action; }
  get elements() { return {namedItem: name => this.querySelector(`[name='${name}']`)}; }
  get required() { return "required" in this.attrs; }
  set required(value) { if (value) this.attrs.required = ""; else delete this.attrs.required; }
  set className(value) { this.attrs.class = value; }
  get className() { return this.attrs.class || ""; }
  appendChild(child) { this.children.push(child); if (typeof child !== "string") child.parentElement = this; return child; }
  append(...children) { children.forEach(c => this.appendChild(c)); }
  replaceChildren(...children) { for (const c of this.children) if (typeof c !== "string") c.parentElement = null; this.children = []; this.append(...children); }
  remove() { this.parentElement.children = this.parentElement.children.filter(c => c !== this); this.parentElement = null; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return key in this.attrs ? this.attrs[key] : null; }
  removeAttribute(key) { delete this.attrs[key]; }
  matches(selector) {
    const tag = selector.match(/^[a-z][a-z0-9]*/i);
    if (tag && this.tagName !== tag[0].toUpperCase()) return false;
    const id = selector.match(/#([\w-]+)/);
    if (id && this.attrs.id !== id[1]) return false;
    const cls = selector.split("[")[0].match(/\.([\w-]+)/);
    if (cls && !this.className.split(/\s+/).includes(cls[1])) return false;
    for (const match of selector.matchAll(/\[([\w-]+)(?:=['"]?([^'"\]]*)['"]?)?\]/g)) {
      if (!(match[1] in this.attrs) || (match[2] !== undefined && this.attrs[match[1]] !== match[2])) return false;
    }
    return true;
  }
  querySelectorAll(selector) {
    return this.children.filter(c => typeof c !== "string").flatMap(c => [
      ...(c.matches(selector) ? [c] : []), ...c.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
  addEventListener(type, callback) { (this.handlers[type] ||= []).push(callback); }
  focus() { this.focused = true; }
  async dispatch(type) {
    const event = {target: this, defaultPrevented: false, preventDefault() {this.defaultPrevented = true;}};
    for (const handler of this.handlers[type] || []) await handler(event);
    return event;
  }
}
function deserialize(node) { return typeof node === "string" ? node : new Element(node.tag, node.attrs, node.children.map(deserialize)); }
const root = deserialize(input.document);
const document = {querySelector: s => root.querySelector(s), querySelectorAll: s => root.querySelectorAll(s),
  getElementById: id => root.querySelector("#" + id), createElement: tag => new Element(tag)};
const saved = new Map(Object.entries(input.saved));
const storage = {getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value)};
let currentUrl = new URL(input.location);
const navigation = [];
const windowHandlers = {};
const location = {get href() {return currentUrl.href;}, get search() {return currentUrl.search;},
  assign() {throw new Error("page navigation is forbidden");}, reload() {throw new Error("page reload is forbidden");}};
const history = {pushState(_state, _title, url) {navigation.push(String(url)); currentUrl = new URL(url, currentUrl);},
  replaceState(_state, _title, url) {currentUrl = new URL(url, currentUrl);}};
const window = {location, history, addEventListener(type, fn) {windowHandlers[type] = fn;}};
const requests = [], responses = [];
const fetch = async (url, options) => {
  requests.push({url, options});
  if (input.serverOrigin) {
    const target = new URL(url, input.serverOrigin);
    assert.equal(target.origin, input.serverOrigin, "only the isolated board may receive requests");
    return globalThis.fetch(target, {...options, signal: AbortSignal.timeout(5000)});
  }
  const reply = responses.shift();
  assert.ok(reply, "unexpected client request");
  if (reply instanceof Error) throw reply;
  return {ok: reply.status >= 200 && reply.status < 300, status: reply.status, json: async () => reply.body};
};
vm.runInNewContext(input.script, {document, localStorage: storage, fetch, window, history, location, URL});
const q = s => document.querySelector(s);
const qa = s => document.querySelectorAll(s);
const identity = q("#review-identity");
const marker = identity.querySelector("[name='marker']"), token = identity.querySelector("[name='token']");
const form = q(".review-form"), card = form.closest(".scenario-card");
const field = name => form.elements.namedItem(name);
const button = status => form.querySelector(`[data-mark-status='${status}']`);
const message = form.querySelector("[data-review-message]");
const expanded = form.querySelector("[data-review-fields]");
const submit = form.querySelector("button[type='submit']");
const click = element => element.dispatch("click");
function fillIdentity() {marker.value = "Tester A"; token.value = "local-test-value";}
function reply(status = "pass", round = 2) {
  const link = status === "pass" ? "" : "https://github.com/demo-org/demo-app/issues/7";
  return {status: 200, body: {ok: true, round, filters: {all: 4, unreviewed: 3, "agent-pass": 2,
    fail: status === "fail" ? 2 : 1, blocked: status === "blocked" ? 2 : 1}, scenario: {
      scenario: field("scenario").value, fingerprint: field("fingerprint").value, status,
      queue: status === "pass" ? "reviewed" : "issues", latest_agent: {status: "pass"},
      latest_manual: {status, number: round, executor: "Tester A", executed_at: "2020-01-15T00:09:10+00:00",
        executed_at_display: "2020-01-15 08:09:10 +08:00", finding: link, note: status === "pass" ? "" : "保留备注",
        actual: status === "pass" ? "人工复核通过" : `人工复核不通过（${status === "fail" ? "失败" : "受阻"}），缺陷 ${link}`,
        evidence: [{name: `observation-${round}.json`, sha256: "a".repeat(64)}]}}}};
}
function assertSaved(status, round = 2) {
  assert.match(message.textContent, new RegExp("已保存.*" + round));
  const latest = card.querySelector("[data-latest-manual]");
  assert.match(latest.textContent, /Tester A/);
  assert.match(latest.textContent, /2020-01-15 08:09:10 \+08:00/);
  assert.ok(latest.querySelector("p").textContent.startsWith("人工 · " + {pass: "通过", fail: "不通过（失败）", blocked: "不通过（受阻）"}[status] + " · 标记人："));
  assert.equal(card.querySelector("[data-queue]").textContent, "所在队列：" + (status === "pass" ? "已复核通过" : "问题"));
  assert.equal(expanded.hidden, true);
  assert.equal(submit.disabled, false);
  assert.equal(currentUrl.href, input.location);
}
async function main() {
  if (input.case.startsWith("real-") || input.case === "seven-filters") {
    await runBusinessCase();
  } else if (input.case === "identity-auto") {
    marker.value = "张三"; await marker.dispatch("input");
    token.value = "local-test-value"; await token.dispatch("input");
    assert.equal(saved.get("sop-board.marker"), "张三");
    assert.equal(saved.get("sop-board.token"), "local-test-value");
    const freshRoot = deserialize(input.document);
    const freshDocument = {querySelector: s => freshRoot.querySelector(s), querySelectorAll: s => freshRoot.querySelectorAll(s),
      getElementById: id => freshRoot.querySelector("#" + id), createElement: tag => new Element(tag)};
    vm.runInNewContext(input.script, {document: freshDocument, localStorage: storage, fetch, window, history, location, URL});
    assert.equal(freshDocument.querySelector("[name='marker']").value, "张三");
    assert.equal(freshDocument.querySelector("[name='token']").value, "local-test-value");
    marker.value = "李四"; await marker.dispatch("input");
    assert.equal(saved.get("sop-board.marker"), "李四");
    assert.equal(requests.length, 0);
  } else if (input.case === "identity") {
    assert.equal(marker.value, "Saved Tester"); assert.equal(token.value, "local-test-value");
    marker.value = "Changed Tester"; token.value = "changed-test-value";
    await click(q("#save-identity"));
    assert.equal(saved.get("sop-board.marker"), "Changed Tester");
    assert.equal(saved.get("sop-board.token"), "changed-test-value");
    assert.match(q("#identity-message").textContent, /已保存/);
    marker.value = "Third Tester"; await marker.dispatch("change");
    assert.equal(saved.get("sop-board.marker"), "Third Tester");
    assert.equal(requests.length, 0);
  } else if (input.case === "expand") {
    fillIdentity(); assert.equal(expanded.hidden, true);
    await click(button("fail")); assert.equal(expanded.hidden, false);
    assert.equal(button("fail").getAttribute("aria-expanded"), "true");
    field("note").value = "保留备注";
    await click(button("blocked")); assert.equal(expanded.hidden, false);
    assert.equal(field("note").value, "保留备注");
    assert.equal(button("blocked").getAttribute("aria-expanded"), "true");
    await click(form.querySelector("[data-review-cancel]"));
    assert.equal(expanded.hidden, true); assert.equal(field("note").value, "");
    assert.equal(field("defect_url").value, ""); assert.equal(message.textContent, "");
    assert.equal(requests.length, 0);
  } else if (input.case.startsWith("missing-identity-")) {
    const status = input.case.slice(17);
    await click(button(status));
    assert.equal(message.textContent, "请先在身份栏填写标记人与写入令牌");
    assert.equal(expanded.hidden, true); assert.equal(requests.length, 0);
    marker.value = "Tester A"; await click(button(status));
    assert.equal(message.textContent, "请先在身份栏填写标记人与写入令牌");
    marker.value = ""; token.value = "local-test-value"; await click(button(status));
    assert.equal(message.textContent, "请先在身份栏填写标记人与写入令牌");
    assert.equal(requests.length, 0);
  } else if (input.case.startsWith("empty-defect-")) {
    fillIdentity(); await click(button(input.case.slice(13)));
    field("defect_url").value = input.case.endsWith("fail") ? "   " : ""; field("note").value = "保留备注";
    const event = await form.dispatch("submit");
    assert.equal(event.defaultPrevented, true);
    assert.equal(message.textContent, "请填写缺陷链接");
    assert.equal(field("note").value, "保留备注"); assert.equal(expanded.hidden, false);
    assert.equal(requests.length, 0);
  } else if (input.case.startsWith("success-") || ["detail-success", "filtered-success"].includes(input.case)) {
    fillIdentity(); const status = input.case.startsWith("success-") ? input.case.slice(8) : "pass";
    responses.push(reply(status));
    const beforeAgent = card.querySelector("[data-latest-agent]").textContent;
    if (status === "pass") await click(button(status));
    else {
      await click(button(status)); field("defect_url").value = "ABC-1"; field("note").value = "保留备注";
      const event = await form.dispatch("submit"); assert.equal(event.defaultPrevented, true);
    }
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, form.action);
    assert.equal(requests[0].options.method, "POST");
    assert.equal(requests[0].options.headers.Authorization, "Bearer local-test-value");
    const expected = {scenario: field("scenario").value, fingerprint: field("fingerprint").value, status, marker: "Tester A"};
    if (status !== "pass") Object.assign(expected, {defect_url: "ABC-1", note: "保留备注"});
    assert.deepEqual(JSON.parse(requests[0].options.body), expected);
    assertSaved(status);
    assert.equal(card.querySelector("[data-latest-agent]").textContent, beforeAgent);
    if (input.case === "detail-success") {
      const history = q("[data-result-history]");
      assert.match(history.textContent, /第 2 轮.*人工.*通过/);
      assert.match(history.textContent, /人工复核通过/);
      assert.ok(history.querySelector("a[download='observation-2.json']"));
      assert.equal(history.querySelectorAll(".result").length, 2);
    } else {
      assert.equal(q("[data-filter='unreviewed']").querySelector("span").textContent, "3");
      assert.equal(q("[data-filter='all']").querySelector("span").textContent, "4");
      assert.equal(q("[data-filter='agent-pass']").querySelector("span").textContent, "2");
      assert.equal(q("[data-filter='fail']").querySelector("span").textContent, status === "fail" ? "2" : "1");
      assert.equal(q("[data-filter='blocked']").querySelector("span").textContent, status === "blocked" ? "2" : "1");
      assert.equal(qa(".scenario-card")[1].querySelector("[data-latest-manual]").textContent, "");
      if (input.case === "filtered-success") assert.equal(card.hidden, true);
    }
  } else if (input.case.startsWith("reject-")) {
    fillIdentity(); await click(button("fail")); field("defect_url").value = "ABC-1"; field("note").value = "保留备注";
    const kind = input.case.slice(7);
    if (kind === "network") responses.push(new Error("connection interrupted"));
    else responses.push({status: kind === "unauthorized" ? 401 : kind === "stale" ? 409 : 422,
      body: {ok: false, error_code: kind === "unauthorized" ? "unauthorized" : kind === "stale" ? "scenario-stale" : "defect-link-invalid",
        problems: [{field: "defect_url", problem: "具体拒绝原因 <script>unsafe</script>"},
                   {field: "note", problem: "备注具体问题"}]}});
    await form.dispatch("submit");
    assert.equal(field("defect_url").value, "ABC-1"); assert.equal(field("note").value, "保留备注");
    assert.equal(expanded.hidden, false); assert.equal(submit.disabled, false);
    assert.equal(message.dataset.state, "error");
    assert.equal(message.children.every(c => typeof c === "string"), true);
    if (kind === "unauthorized") {assert.match(message.textContent, /令牌无效/); assert.equal(token.focused, true);}
    else if (kind === "network") assert.match(message.textContent, /网络.*保留/);
    else assert.match(message.textContent, /具体拒绝原因/);
    if (kind !== "network") {
      assert.ok(message.textContent.includes(kind === "unauthorized" ? "unauthorized" : kind === "stale" ? "scenario-stale" : "defect-link-invalid"));
      assert.match(message.textContent, /具体拒绝原因/);
      assert.match(message.textContent, /备注具体问题/);
    }
    responses.push(reply("fail", 3)); await form.dispatch("submit"); assertSaved("fail", 3);
    assert.equal(requests.length, 2);
  } else if (input.case === "filters") {
    const visible = () => qa(".scenario-card").filter(c => !c.hidden).map(c => c.dataset.scenario);
    const expected = {all: ["N1-P01", "N1-N01", "N2-P01", "N2-N01"], unreviewed: ["N1-P01", "N1-N01", "N2-P01", "N2-N01"],
      "agent-pass": ["N1-P01", "N2-P01"], fail: ["N1-N01"], blocked: ["N2-N01"]};
    const initial = currentUrl.searchParams.get("filter");
    assert.deepEqual(visible(), expected[initial] || expected.all);
    for (const [filter, ids] of Object.entries(expected)) {
      const event = await click(q(`[data-filter='${filter}']`));
      assert.equal(event.defaultPrevented, true); assert.deepEqual(visible(), ids);
      assert.equal(currentUrl.searchParams.get("filter"), filter);
      assert.equal(currentUrl.searchParams.get("keep"), "yes"); assert.equal(currentUrl.hash, "#anchor");
    }
    assert.equal(q("[data-node='N1']").hidden, true); assert.equal(q("[data-node='N2']").hidden, false);
    currentUrl = new URL(input.location); await windowHandlers.popstate();
    assert.deepEqual(visible(), expected[initial] || expected.all);
    assert.equal(requests.length, 0);
  } else throw new Error("unknown case " + input.case);
  process.stdout.write("client case passed: " + input.case + "\n");
}
function controls(sid, doc = document) {
  const card = doc.querySelector(`[data-scenario='${sid}']`);
  const form = card.querySelector(".review-form");
  return {card, form, field: name => form.elements.namedItem(name),
    button: status => form.querySelector(`[data-mark-status='${status}']`),
    expanded: form.querySelector("[data-review-fields]"), message: form.querySelector("[data-review-message]")};
}
function counts() {
  return Object.fromEntries(qa("[data-filter]").map(link => [link.dataset.filter, Number(link.querySelector("span").textContent)]));
}
function readyIdentity(name = "张三") {
  marker.value = name; token.value = "isolated-test";
}
async function serverGet(path) {
  const response = await globalThis.fetch(new URL(path, input.serverOrigin), {signal: AbortSignal.timeout(5000)});
  return {status: response.status, body: await response.json()};
}
function assertResult(c, status, name = "张三") {
  const manual = c.card.querySelector("[data-latest-manual]");
  const label = {pass: "通过", fail: "不通过（失败）", blocked: "不通过（受阻）"}[status];
  assert.ok(manual.querySelector("p").textContent.startsWith("人工 · " + label + " · 标记人：" + name));
  assert.match(manual.querySelector("time").textContent, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{2}:\d{2}$/);
  assert.equal(c.card.querySelector("[data-queue]").textContent, "所在队列：" + (status === "pass" ? "已复核通过" : "问题"));
  assert.equal(c.expanded.hidden, true);
  assert.equal(currentUrl.href, input.location);
}
async function runBusinessCase() {
  if (input.case === "seven-filters") {
    const initial = new URL(input.location).searchParams.get("filter") || "all";
    const expected = input.changed ? {
      all: ["N1-P01", "N1-P02", "N1-N01", "N1-N02", "N2-P01", "N2-P02"],
      unreviewed: ["N1-P01", "N1-P02", "N1-N01", "N1-N02"], "agent-pass": ["N2-P01", "N2-P02"],
      fail: ["N1-N01", "N2-P02"], blocked: ["N1-N02"]} : {
      all: ["N1-P01", "N1-P02", "N1-N01", "N1-N02", "N2-P01", "N2-P02", "N2-N01"],
      unreviewed: ["N1-P01", "N1-P02", "N1-N01", "N1-N02", "N2-N01"],
      "agent-pass": ["N1-P02", "N2-P01", "N2-P02"], fail: ["N1-N01", "N2-P02"], blocked: ["N1-N02"]};
    const visible = () => qa(".scenario-card").filter(card => !card.hidden).map(card => card.dataset.scenario);
    assert.deepEqual(visible(), expected[initial] || expected.all);
    assert.deepEqual(counts(), Object.fromEntries(Object.entries(expected).map(([key, ids]) => [key, ids.length])));
    const active = expected[initial] ? initial : "all";
    assert.equal(q(`[data-filter='${active}']`).getAttribute("aria-current"), "true");
    for (const key of ["unreviewed", "agent-pass", "fail", "blocked", "all"]) {
      await click(q(`[data-filter='${key}']`));
      assert.deepEqual(visible(), expected[key]);
      assert.equal(currentUrl.search, "?filter=" + key);
      assert.equal(qa("[data-filter]").filter(link => link.getAttribute("aria-current")).length, 1);
      assert.equal(q(`[data-filter='${key}']`).getAttribute("aria-current"), "true");
    }
    assert.equal(requests.length, 0);
    return;
  }
  readyIdentity();
  if (input.case === "real-seven-pass" || input.case === "real-seven-nonpass") {
    const beforeAgents = Object.fromEntries(qa(".scenario-card").map(card => [card.dataset.scenario, card.querySelector("[data-latest-agent]").textContent]));
    assert.deepEqual(counts(), {all: 7, unreviewed: 5, "agent-pass": 3, fail: 2, blocked: 1});
    const first = controls("N1-P02"), second = controls("N1-P01");
    if (input.case === "real-seven-pass") {
      await click(first.button("pass")); assertResult(first, "pass");
      assert.deepEqual(counts(), {all: 7, unreviewed: 4, "agent-pass": 3, fail: 2, blocked: 1});
      await click(second.button("pass")); assertResult(second, "pass");
      assert.deepEqual(counts(), {all: 7, unreviewed: 3, "agent-pass": 3, fail: 2, blocked: 1});
    } else {
      for (const [c, status, issue, note] of [[first, "fail", "ABC-123", ""], [second, "blocked", "ABC-124", "测试环境无法登录"]]) {
        await click(c.button(status)); assert.equal(c.expanded.hidden, false);
        c.field("defect_url").value = issue; c.field("note").value = note;
        const event = await c.form.dispatch("submit"); assert.equal(event.defaultPrevented, true);
        assertResult(c, status);
        assert.ok(c.card.querySelector(`[href='https://acme.atlassian.net/browse/${issue}']`));
      }
      assert.deepEqual(counts(), {all: 7, unreviewed: 3, "agent-pass": 3, fail: 3, blocked: 2});
      assert.equal("note" in JSON.parse(requests[0].options.body), false);
      assert.equal(JSON.parse(requests[1].options.body).note, "测试环境无法登录");
    }
    assert.equal(requests.length, 2);
    for (const card of qa(".scenario-card")) assert.equal(card.querySelector("[data-latest-agent]").textContent, beforeAgents[card.dataset.scenario]);
    for (const request of requests) assert.deepEqual(Object.keys(JSON.parse(request.options.body)).sort(),
      (JSON.parse(request.options.body).status === "pass" ? ["scenario", "fingerprint", "status", "marker"] :
       JSON.parse(request.options.body).note ? ["scenario", "fingerprint", "status", "marker", "defect_url", "note"] :
       ["scenario", "fingerprint", "status", "marker", "defect_url"]).sort());
  } else if (input.case === "real-empty-cancel") {
    const first = controls("N1-P01"), second = controls("N2-P01");
    const before = counts(), original = qa("[data-latest-manual]").map(node => node.textContent);
    await click(first.button("blocked")); first.field("note").value = "等环境";
    await first.form.dispatch("submit"); assert.equal(first.message.textContent, "请填写缺陷链接");
    await click(second.button("fail")); second.field("defect_url").value = "   ";
    await second.form.dispatch("submit"); assert.equal(second.message.textContent, "请填写缺陷链接");
    await click(second.form.querySelector("[data-review-cancel]"));
    assert.equal(second.expanded.hidden, true); assert.equal(first.field("note").value, "等环境");
    assert.equal(Array.from(second.form.querySelectorAll("[data-mark-status]")).every(button => !button.disabled), true);
    assert.equal(requests.length, 0); assert.deepEqual(counts(), before);
    assert.deepEqual(qa("[data-latest-manual]").map(node => node.textContent), original);
  } else if (input.case.startsWith("real-detail-")) {
    const status = input.case.slice(12), c = controls(input.sid);
    const before = q("[data-result-history]").querySelectorAll(".result").length;
    if (status === "pass") await click(c.button("pass"));
    else {await click(c.button("fail")); c.field("defect_url").value = "ABC-77"; await c.form.dispatch("submit");}
    assertResult(c, status);
    const result = q("[data-result-history]").querySelectorAll(".result").at(-1);
    assert.equal(result.querySelector("h3").textContent, "第 2 轮 · 人工 · " + (status === "pass" ? "通过" : "不通过（失败）"));
    assert.ok(result.textContent.includes("张三"));
    assert.ok(result.querySelector("a[download='observation-2.json']"));
    assert.equal(q("[data-result-history]").querySelectorAll(".result").length, before + 1);
    assert.equal(requests.length, 1);
  } else if (input.case === "real-retry") {
    const c = controls("N2-P01"), before = counts(), original = qa("[data-latest-manual]").map(n => n.textContent);
    const originalOther = controls("N1-P01").card.textContent;
    token.value = "wrong-isolated-value"; await click(c.button("fail"));
    c.field("defect_url").value = "https://github.com/demo-org/demo-app/issues/7"; c.field("note").value = "截图见附件";
    await c.form.dispatch("submit");
    assert.match(c.message.textContent, /unauthorized/); assert.match(c.message.textContent, /令牌缺失或不符/);
    assert.equal(c.field("defect_url").value, "https://github.com/demo-org/demo-app/issues/7");
    assert.equal((await serverGet(input.api + "/rounds/2")).status, 404);
    assert.deepEqual(counts(), before); assert.deepEqual(qa("[data-latest-manual]").map(n => n.textContent), original);
    token.value = "isolated-test"; c.field("defect_url").value = "ABC-123";
    await c.form.dispatch("submit");
    assert.match(c.message.textContent, /defect-link-invalid/); assert.match(c.message.textContent, /完整链接/);
    assert.equal(c.field("defect_url").value, "ABC-123"); assert.equal(c.field("note").value, "截图见附件");
    assert.equal((await serverGet(input.api + "/rounds/2")).status, 404);
    assert.deepEqual(counts(), before); assert.deepEqual(qa("[data-latest-manual]").map(n => n.textContent), original);
    assert.equal(controls("N1-P01").card.textContent, originalOther);
    c.field("defect_url").value = "https://github.com/demo-org/demo-app/issues/7";
    await c.form.dispatch("submit"); assertResult(c, "fail"); assert.equal(requests.length, 3);
  } else if (input.case === "real-stale") {
    const c = controls("N2-P01"), before = counts(), original = c.card.querySelector("[data-latest-manual]").textContent;
    await click(c.button("fail")); c.field("defect_url").value = "ABC-9"; c.field("note").value = "保留草稿";
    const changed = await globalThis.fetch(new URL(input.api + "/scenario-file", input.serverOrigin), {
      method: "PUT", headers: {"Content-Type": "application/json", Authorization: "Bearer isolated-test"},
      body: JSON.stringify(input.mutation), signal: AbortSignal.timeout(5000)});
    assert.equal(changed.status, 200);
    await c.form.dispatch("submit"); assert.match(c.message.textContent, /scenario-stale/);
    assert.match(c.message.textContent, /指纹已变更/);
    assert.equal(c.field("defect_url").value, "ABC-9"); assert.equal(c.field("note").value, "保留草稿");
    assert.equal(c.expanded.hidden, false); assert.equal(requests.length, 1);
    assert.deepEqual(counts(), before); assert.equal(c.card.querySelector("[data-latest-manual]").textContent, original);
    assert.equal((await serverGet(input.api + "/rounds/4")).status, 404);
  } else if (input.case === "real-identity") {
    marker.value = "张三"; token.value = ""; await marker.dispatch("input");
    await click(controls("N1-P01").button("pass"));
    assert.equal(controls("N1-P01").message.textContent, "请先在身份栏填写标记人与写入令牌");
    assert.equal(requests.length, 0);
    token.value = "isolated-test"; await token.dispatch("input");
    const freshRoot = deserialize(input.document);
    const freshDocument = {querySelector: s => freshRoot.querySelector(s), querySelectorAll: s => freshRoot.querySelectorAll(s),
      getElementById: id => freshRoot.querySelector("#" + id), createElement: tag => new Element(tag)};
    vm.runInNewContext(input.script, {document: freshDocument, localStorage: storage, fetch, window, history, location, URL});
    assert.equal(freshDocument.querySelector("[name='marker']").value, "张三");
    assert.equal(freshDocument.querySelector("[name='token']").value, "isolated-test");
    await click(controls("N1-P01", freshDocument).button("pass"));
    const newMarker = freshDocument.querySelector("[name='marker']"); newMarker.value = "李四"; await newMarker.dispatch("input");
    await click(controls("N2-P01", freshDocument).button("pass"));
    assert.equal(saved.get("sop-board.marker"), "李四"); assert.equal(saved.get("sop-board.token"), "isolated-test");
    assert.equal(JSON.parse(requests[0].options.body).marker, "张三");
    assert.equal(JSON.parse(requests[1].options.body).marker, "李四"); assert.equal(requests.length, 2);
  } else throw new Error("unknown real business case " + input.case);
  assert.equal(currentUrl.href, input.location);
}
main().catch(error => {console.error(error); process.exitCode = 1;});
