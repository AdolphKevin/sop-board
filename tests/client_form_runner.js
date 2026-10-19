// Isolated client-logic tests: no browser and no network requests.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const nodes = {};
function control(value = "", required = false) {
  return { value, required, disabled: false, textContent: "", dataset: {}, handlers: {}, captures: {},
    addEventListener(type, callback, capture = false) { this.handlers[type] = callback; this.captures[type] = capture; },
    focus() { this.focused = true; } };
}
const fields = Object.fromEntries(input.fields.map(f => [f.name, control(f.value, f.required)]));
const form = control();
form.action = input.action;
form.elements = { namedItem: name => fields[name] };
form.querySelector = () => nodes.submit;
form.reportValidity = () => {
  const invalid = Object.values(fields).filter(f => f.required && !f.value.trim());
  // Native invalid events do not bubble; a form can observe them in capture.
  for (const target of invalid) {
    if (form.captures.invalid) {
      form.handlers.invalid({target, preventDefault() { this.defaultPrevented = true; }});
    }
  }
  return invalid.length === 0;
};
nodes["mark-form"] = form;
nodes["mark-message"] = control();
nodes["save-identity"] = control();
nodes.submit = control();
const saved = new Map(Object.entries(input.saved || {}));
const storage = { getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value) };
const requests = [];
const responses = [];
const fetch = async (url, options) => {
  requests.push({url, options});
  const reply = responses.shift();
  if (reply instanceof Error) throw reply;
  assert.ok(reply, "unexpected client request");
  return {ok: reply.status >= 200 && reply.status < 300, status: reply.status, json: async () => reply.body};
};
vm.runInNewContext(input.script, {document: {getElementById: id => nodes[id] || null}, localStorage: storage, fetch});

async function main() {
  if (input.case === "identity") {
    assert.equal(fields.marker.value, "Saved Tester", "saved marker was not restored");
    assert.equal(fields.token.value, "local-test-value", "saved token was not restored");
    fields.marker.value = "Changed Tester";
    fields.token.value = "changed-test-value";
    assert.ok(nodes["save-identity"].handlers.click, "identity save is not handled");
    await nodes["save-identity"].handlers.click();
    assert.equal(saved.get("sop-board.marker"), "Changed Tester");
    assert.equal(saved.get("sop-board.token"), "changed-test-value");
    assert.match(nodes["mark-message"].textContent, /已保存/);
    assert.equal(requests.length, 0);
  } else if (input.case === "constraints") {
    assert.ok(fields.status.handlers.change, "status requirements are not updated");
    fields.status.value = "fail";
    await fields.status.handlers.change();
    assert.equal(fields.defect_url.required, true);
    fields.status.value = "blocked";
    await fields.status.handlers.change();
    assert.equal(fields.finding.required, true);
    assert.equal(fields.defect_url.required, false);
    fields.status.value = "skip";
    await fields.status.handlers.change();
    assert.equal(fields.finding.required, true);
    fields.status.value = "pass";
    await fields.status.handlers.change();
    assert.equal(fields.finding.required, false);
    assert.equal(fields.defect_url.required, false);
    assert.equal(requests.length, 0);
  } else if (input.case.startsWith("empty-defect-")) {
    fields.marker.value = "Tester A";
    fields.token.value = "local-test-value";
    fields.actual.value = "Keep my observation";
    fields.environment.value = "Keep my environment";
    fields.note.value = "Keep my note";
    fields.status.value = "fail";
    await fields.status.handlers.change();
    const before = Object.fromEntries(Object.entries(fields).map(([k, v]) => [k, v.value]));
    if (input.case === "empty-defect-native") {
      // Browser constraint validation precedes submit and can suppress it.
      if (form.reportValidity()) await form.handlers.submit({preventDefault() {}});
    } else {
      await form.handlers.submit({preventDefault() {}});
    }
    assert.equal(nodes["mark-message"].textContent, "失败结果必须填写本项目的缺陷链接");
    assert.equal(nodes["mark-message"].dataset.state, "error");
    assert.deepEqual(Object.fromEntries(Object.entries(fields).map(([k, v]) => [k, v.value])), before);
    assert.equal(requests.length, 0);
    assert.equal(nodes.submit.disabled, false);
  } else if (input.case === "success") {
    fields.marker.value = "Tester A";
    fields.token.value = "local-test-value";
    fields.actual.value = "Observed result";
    fields.environment.value = "isolated environment";
    fields.note.value = "review note";
    responses.push({status: 200, body: {ok: true, round: 2}});
    assert.ok(form.handlers.submit, "form submission is not handled");
    let prevented = false;
    await form.handlers.submit({preventDefault() {prevented = true;}});
    assert.equal(prevented, true);
    assert.equal(requests.length, 1);
    const request = requests[0];
    assert.equal(request.url, input.action);
    assert.equal(request.options.method, "POST");
    assert.equal(request.options.headers.Authorization, "Bearer local-test-value");
    assert.deepEqual(JSON.parse(request.options.body), {scenario: fields.scenario.value,
      fingerprint: fields.fingerprint.value, status: "pass", actual: "Observed result",
      marker: "Tester A", environment: "isolated environment", evidence: [], note: "review note"});
    assert.equal(saved.get("sop-board.marker"), "Tester A");
    assert.equal(nodes.submit.disabled, false);
    assert.match(nodes["mark-message"].textContent, /已保存.*2/);
    assert.equal(nodes["mark-message"].dataset.state, "success");
  } else if (input.case.startsWith("reject-")) {
    fields.marker.value = "Tester A";
    fields.token.value = "local-test-value";
    fields.actual.value = "Keep my observation";
    fields.environment.value = "Keep my environment";
    fields.status.value = "fail";
    fields.defect_url.value = "https://example.com/bug/1";
    fields.note.value = "Keep my note";
    const kind = input.case.slice(7);
    const codes = {defect: [422, "defect-link-required"], unauthorized: [401, "unauthorized"], stale: [409, "scenario-stale"]};
    if (kind === "network") responses.push(new Error("connection interrupted"));
    else responses.push({status: codes[kind][0], body: {ok: false, error_code: codes[kind][1],
      problems: [{field: "defect_url", problem: "具体拒绝原因 <script>unsafe</script>"}]}});
    const before = Object.fromEntries(Object.entries(fields).map(([k, v]) => [k, v.value]));
    await form.handlers.submit({preventDefault() {}});
    assert.deepEqual(Object.fromEntries(Object.entries(fields).map(([k, v]) => [k, v.value])), before);
    assert.equal(nodes.submit.disabled, false);
    assert.equal(nodes["mark-message"].dataset.state, "error");
    if (kind === "unauthorized") {
      assert.match(nodes["mark-message"].textContent, /令牌.*重新填写/);
      assert.equal(fields.token.focused, true);
    } else if (kind === "network") assert.match(nodes["mark-message"].textContent, /网络.*保留/);
    else assert.match(nodes["mark-message"].textContent, /具体拒绝原因/);
    fields.defect_url.value = "https://github.com/demo-org/demo-app/issues/1";
    responses.push({status: 200, body: {ok: true, round: 3}});
    await form.handlers.submit({preventDefault() {}});
    assert.equal(requests.length, 2);
    assert.match(nodes["mark-message"].textContent, /已保存.*3/);
  } else {
    throw new Error("unknown test case");
  }
  process.stdout.write("client case passed: " + input.case + "\n");
}
main().catch(error => {console.error(error); process.exitCode = 1;});
