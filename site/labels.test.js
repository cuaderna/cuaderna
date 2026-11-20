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

function runFileText(attempt) {
  const run = { run_id: "r", name: "f", status: "completed", created_at: "2026-11-19T22:31:16Z" };
  const base = { step_key: "s#1", name: "s", status: "ok", started_at: "", finished_at: "" };
  return JSON.stringify({ format: "cuaderna.run/1", run, attempts: [{ ...base, ...attempt }] });
}

test("annotations that are not an object are refused when the file is read", () => {
  for (const annotations of ["x", 5, true, [], [1]]) {
    assert.throws(() => labels.parseRunFile(runFileText({ annotations })), /not a run file/);
  }
  for (const annotations of [undefined, null, {}, { a: 1 }]) {
    assert.equal(labels.parseRunFile(runFileText({ annotations })).attempts.length, 1);
  }
});

test("a file that is not JSON or not a run file is refused with a message", () => {
  assert.throws(() => labels.parseRunFile("{"), /not JSON/);
  assert.throws(() => labels.parseRunFile("[]"), /not a run file/);
  assert.throws(() => labels.parseRunFile("null"), /not a run file/);
});

test("a file over the size limit is refused before it is read", () => {
  assert.equal(labels.fileSizeProblem(0), null);
  assert.equal(labels.fileSizeProblem(16 * 1024 * 1024), null);
  assert.match(labels.fileSizeProblem(16 * 1024 * 1024 + 1), /larger than the 16 MiB/);
});

test("an amount with an absurd number of digits is an unknown cost", () => {
  assert.equal(labels.costText("1".repeat(100000)), "unknown");
  assert.equal(labels.costText("0." + "1".repeat(100000)), "unknown");
  assert.equal(labels.costText("1".repeat(30) + "." + "1".repeat(30)), "1".repeat(30) + "." + "1".repeat(30) + " USD");
});

test("adding a very long list of amounts does not overflow the stack", () => {
  assert.equal(labels.sumOfAmounts(new Array(300000).fill("1")), "300000");
});

function runFile(run, attempts, format = "cuaderna.run/2") {
  return { format, run: { run_id: "r", name: "n", status: "completed", created_at: "t", ...run }, attempts };
}

function attemptOf(stepKey, annotations, extra = {}) {
  return { step_key: stepKey, name: stepKey, status: "ok", started_at: "t", finished_at: "t", attempt: 1, annotations, ...extra };
}

const experiment = { source_run_id: "src", reuse: ["a", "b"], plan_id: "p", plan_hash: "h", fault_id: "model-timeout" };
const injected = { plan_id: "p", plan_hash: "h", fault_id: "model-timeout", kind: "raise", executed: false };
const reused = { run_id: "src", step_key: "a", attempt: 1 };
const denial = { decision: "deny", effect: "deny", reason: "no_rule" };

test("a run file in either format is accepted and another is not", () => {
  for (const format of ["cuaderna.run/1", "cuaderna.run/2"]) {
    assert.equal(labels.parseRunFile(JSON.stringify(runFile({}, [], format))).attempts.length, 0);
  }
  assert.throws(() => labels.parseRunFile(JSON.stringify(runFile({}, [], "cuaderna.run/3"))), /not a run file/);
});

test("the two row labels follow the annotations and can come together", () => {
  assert.deepEqual(labels.attemptMarks(attemptOf("a", {})), []);
  assert.deepEqual(labels.attemptMarks(attemptOf("a", { "cuaderna.fault": injected })), ["injected"]);
  assert.deepEqual(labels.attemptMarks(attemptOf("a", { "cuaderna.reuse": reused })), ["from the recording"]);
  const both = attemptOf("a", { "cuaderna.fault": injected, "cuaderna.reuse": reused });
  assert.deepEqual(labels.attemptMarks(both), ["injected", "from the recording"]);
});

test("a fault or reuse annotation of the wrong types is unreadable, not a throw", () => {
  const faults = [null, "x", 5, [], {}, { ...injected, kind: "boom" }, { ...injected, executed: "no" }, { ...injected, fault_id: 7 }];
  for (const fault of faults) {
    assert.deepEqual(labels.recordedFault(attemptOf("a", { "cuaderna.fault": fault })), { unreadable: true });
  }
  const reuses = [null, "x", [], {}, { ...reused, attempt: "1" }, { ...reused, run_id: {} }, { ...reused, step_key: null }];
  for (const reuse of reuses) {
    assert.deepEqual(labels.recordedReuse(attemptOf("a", { "cuaderna.reuse": reuse })), { unreadable: true });
  }
  assert.equal(labels.recordedFault(attemptOf("a", undefined)), null);
  assert.equal(labels.recordedReuse(attemptOf("a", null)), null);
});

test("a run that is not an experiment has no experiment line", () => {
  assert.equal(labels.experimentLine(labels.experimentFacts(runFile({}, []))), null);
  assert.equal(labels.experimentLine(labels.experimentFacts(runFile({ experiment: null }, []))), null);
});

test("an experiment section of the wrong types is said to be unreadable", () => {
  const hostile = [
    "x",
    5,
    [],
    {},
    { ...experiment, source_run_id: 1 },
    { ...experiment, reuse: "a" },
    { ...experiment, reuse: [1] },
    { ...experiment, fault_id: 5 },
    { ...experiment, plan_id: {} },
    { ...experiment, plan_hash: undefined },
  ];
  for (const section of hostile) {
    const line = labels.experimentLine(labels.experimentFacts(runFile({ experiment: section }, [attemptOf("a", {})])));
    assert.match(line, /cannot read that part/);
  }
});

test("the line of a control says nothing was changed and names the first step run again", () => {
  const control = { ...experiment, fault_id: null };
  const attempts = [attemptOf("a", { "cuaderna.reuse": reused }), attemptOf("b", {}, { attempt: 2 }), attemptOf("c", {})];
  const line = labels.experimentLine(labels.experimentFacts(runFile({ experiment: control }, attempts)));
  assert.match(line, /^This is an experiment, not something that happened\./);
  assert.match(line, /replays run src\./);
  assert.match(line, /Nothing was changed\./);
  assert.match(line, /executed again for real was b, attempt 2\./);
});

test("the line of a fired fault names the step and skips steps that did not run for real", () => {
  const attempts = [
    attemptOf("a", { "cuaderna.reuse": reused }),
    attemptOf("b", { "cuaderna.fault": injected }),
    attemptOf("b", { "cuaderna.authorization": denial }),
    attemptOf("c", {}),
  ];
  const facts = labels.experimentFacts(runFile({ experiment }, attempts));
  assert.equal(facts.faultStep, "b");
  assert.equal(facts.executedAgainStep, null);
  const line = labels.experimentLine(facts);
  assert.match(line, /Fault model-timeout was applied at step b\./);
  assert.doesNotMatch(line, /executed again/);
});

test("an injected attempt whose step ran counts as run again, one with a fault but no step does not", () => {
  const ran = attemptOf("b", { "cuaderna.fault": { ...injected, executed: true } });
  const facts = labels.experimentFacts(runFile({ experiment }, [ran]));
  assert.equal(facts.executedAgainStep, "b");
  const planned = labels.experimentFacts(runFile({ experiment }, [attemptOf("a", {})]));
  assert.match(labels.experimentLine(planned), /was planned but no step carries it/);
});

test("a fault annotation of the wrong types does not break the experiment line", () => {
  const attempts = [attemptOf("a", { "cuaderna.fault": 5, "cuaderna.reuse": "x" })];
  const line = labels.experimentLine(labels.experimentFacts(runFile({ experiment }, attempts)));
  assert.match(line, /was planned but no step carries it/);
});

function entry(overrides = {}) {
  return { file: "experiments/a.json", run_id: "r", kind: "experiment", fault: "f", trial: 1, ended: "t", outcome: null, ...overrides };
}

function indexOf(...entries) {
  return JSON.stringify(entries);
}

test("an index with a path that leaves the page, names a scheme or is not a json file is invalid", () => {
  const hostile = [
    "../secret.json",
    "a/../b.json",
    "a/..json",
    "/etc/x.json",
    "https://example.com/a.json",
    "javascript:alert(1).json",
    "//example.com/a.json",
    "a//b.json",
    "./a.json",
    "a\\b.json",
    "a b.json",
    "a.json?x=1",
    "a.json#x",
    "a.txt",
    "a.json\n",
    "",
    5,
    null,
    "a".repeat(300) + ".json",
  ];
  for (const file of hostile) {
    assert.throws(() => labels.parseRunIndex(indexOf(entry({ file }))), /not valid/);
  }
  assert.equal(labels.parseRunIndex(indexOf(entry({ file: "experiments/model-timeout_2.json" }))).length, 1);
});

test("an index with a field of the wrong type is invalid as a whole", () => {
  const hostile = [
    { run_id: 5 },
    { run_id: { toString: "x" } },
    { kind: "other" },
    { kind: "__proto__" },
    { fault: 5 },
    { fault: undefined },
    { trial: "1" },
    { trial: 1.5 },
    { ended: null },
    { outcome: 7 },
    { extra: "x" },
  ];
  for (const overrides of hostile) {
    assert.throws(() => labels.parseRunIndex(indexOf(entry(), entry(overrides))), /not valid/);
  }
  for (const text of ["{}", "null", "5", '"x"', '[null]', "[[]]"]) {
    assert.throws(() => labels.parseRunIndex(text), /not valid/);
  }
  assert.throws(() => labels.parseRunIndex("{"), /not JSON/);
});

test("an index of more than 200 entries is invalid", () => {
  assert.equal(labels.parseRunIndex(JSON.stringify(new Array(200).fill(entry()))).length, 200);
  assert.throws(() => labels.parseRunIndex(JSON.stringify(new Array(300).fill(entry()))), /not valid/);
});

test("the picker lists the source, then the control, then experiments grouped by fault", () => {
  const entries = [
    entry({ file: "e/t2.json", fault: "model-timeout", trial: 2, outcome: "fixed" }),
    entry({ file: "e/b1.json", fault: "bad-edit", trial: 1, outcome: "failed" }),
    entry({ file: "e/t1.json", fault: "model-timeout", trial: 1, outcome: null }),
    entry({ file: "control.json", kind: "control", fault: null, trial: null }),
    entry({ file: "sample-run.json", kind: "source", fault: null, trial: null }),
  ];
  const choices = labels.pickerChoices(entries);
  assert.deepEqual(
    choices.map(({ file }) => file),
    ["sample-run.json", "control.json", "e/b1.json", "e/t1.json", "e/t2.json"],
  );
  assert.deepEqual(
    choices.map(({ label }) => label),
    [
      "what was recorded",
      "replayed with nothing changed",
      "bad-edit, trial 1: failed",
      "model-timeout, trial 1",
      "model-timeout, trial 2: fixed",
    ],
  );
});

test("a picker label is text and never changes the file it opens", () => {
  const [choice] = labels.pickerChoices([entry({ fault: "<img src=x onerror=alert(1)>", trial: null })]);
  assert.equal(choice.label, "<img src=x onerror=alert(1)>");
  assert.equal(choice.file, "experiments/a.json");
});
