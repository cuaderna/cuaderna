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

test("a cost that cannot be read is unknown, never zero or blank", () => {
  const hostile = [undefined, null, "", "-1", -1, 0, 5, NaN, Infinity, "NaN", "1e5", "\u0663", "1\n", "1 USD", {}];
  for (const usd of hostile) {
    assert.equal(labels.costText(usd), "unknown");
  }
  assert.equal(labels.costText("0"), "0 USD");
  assert.equal(labels.costText("0.0001774"), "0.0001774 USD");
});

test("parts that do not add up to the cost are caught", () => {
  const parts = { input: "0.0000969", cache_read: "0", cache_write: "0", output: "0.0000805", requests: "0" };
  assert.equal(labels.costPartsAgree("0.0001774", parts), true);
  assert.equal(labels.costPartsAgree("0.00017740", parts), true);
  assert.equal(labels.costPartsAgree("0.0000001", { input: "500", output: "900" }), false);
  assert.equal(labels.costPartsAgree("1", { input: "abc" }), false);
  assert.equal(labels.costPartsAgree("1", null), true);
});

test("a negative, garbage or out-of-range time is invalid, not a short duration", () => {
  const start = "2026-11-19T22:31:16.868182Z";
  assert.equal(labels.durationText(start, "2026-11-19T22:31:15Z"), "invalid time");
  assert.equal(labels.durationText("1", "2"), "invalid time");
  assert.equal(labels.durationText("9999-99-99T00:00:00Z", start), "invalid time");
  assert.equal(labels.durationText(start, 7), "invalid time");
  assert.equal(labels.validSeconds(start, "2026-11-19T22:31:15Z"), null);
});

test("durations that are real are shown as they were", () => {
  const start = "2026-11-19T22:31:16.000Z";
  assert.equal(labels.durationText(start, start), "under 1 ms");
  assert.equal(labels.durationText(start, "2026-11-19T22:31:16.400Z"), "400 ms");
  assert.equal(labels.durationText(start, "2026-11-19T22:31:18.500+00:00"), "2.5 s");
  assert.equal(labels.durationText(start, "2026-11-19T22:33:20Z"), "2 min 4 s");
  assert.equal(labels.durationText(start, ""), "not finished");
});

test("the chain hash is reported as present or not, never as checked", () => {
  assert.equal(labels.chainText("f8aaff95"), "present, not checked here");
  for (const missing of [undefined, null, "", 5, {}]) {
    assert.equal(labels.chainText(missing), "not recorded");
  }
});
