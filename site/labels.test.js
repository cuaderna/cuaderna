const assert = require("node:assert/strict");
const { test } = require("node:test");

require("./labels.js");
const labels = globalThis.cuadernaLabels;

function authorizationOf(annotation) {
  const annotations = annotation === undefined ? {} : { "cuaderna.authorization": annotation };
  return labels.authorizationLabel(labels.recordedAuthorization({ annotations }));
}

const allowed = { decision: "allow", effect: "allow", reason: "rule", approval: null };
const asked = { ...allowed, effect: "ask" };
const approval = { by: "ana", at: "2026-11-19T22:31:16Z", note: null };

test("a status outside the known set never becomes a class name", () => {
  for (const hostile of ["ok error ahead bar takeover", "error\u200b", "__proto__", "constructor", ""]) {
    assert.equal(labels.statusClass(hostile), "unknown");
    assert.equal(labels.statusLabel(hostile), "unknown status");
  }
  assert.equal(labels.statusClass("error"), "error");
  assert.equal(labels.statusLabel("failed"), "failed");
});

test("an authorization that is missing is not recorded, never ok", () => {
  assert.equal(authorizationOf(undefined), "not recorded");
  assert.equal(labels.authorizationLabel(labels.recordedAuthorization({})), "not recorded");
});

test("an authorization without a readable decision is unreadable", () => {
  const hostile = [null, {}, [], "allow", 5, { ...allowed, decision: "deny\u200b" }, { decision: "allow" }];
  for (const annotation of hostile) {
    assert.equal(authorizationOf(annotation), "unreadable");
  }
});

test("approved needs the fields a real approval carries", () => {
  assert.equal(authorizationOf({ ...asked, approval }), "approved");
  for (const bad of [{}, [], "yes", { by: "ana" }, { at: approval.at }, { by: 1, at: 2 }]) {
    assert.equal(authorizationOf({ ...asked, approval: bad }), "unreadable");
  }
});

test("a step the policy sent to a person without an approval is flagged", () => {
  assert.equal(authorizationOf(asked), "no approval recorded");
  assert.equal(authorizationOf(allowed), "allowed");
  assert.equal(authorizationOf({ ...allowed, decision: "deny" }), "denied");
});

test("the chain hash is reported as present or not, never as checked", () => {
  assert.equal(labels.chainText("f8aaff95"), "present, not checked here");
  for (const missing of [undefined, null, "", 5, {}]) {
    assert.equal(labels.chainText(missing), "not recorded");
  }
});
