const RUN_FILE_FORMAT = "cuaderna.run/1";
const SAMPLE_URL = "sample-run.json";
const MODEL_FACTS = [
  ["requested model", "gen_ai.request.model"],
  ["answered by", "gen_ai.response.model"],
  ["input tokens", "gen_ai.usage.input_tokens"],
  ["input tokens read from cache", "gen_ai.usage.cache_read.input_tokens"],
  ["output tokens", "gen_ai.usage.output_tokens"],
];
const COST_PART_LABELS = [
  ["input", "input"],
  ["cache read", "cache_read"],
  ["cache write", "cache_write"],
  ["output", "output"],
  ["requests", "requests"],
];
const AMOUNT_PATTERN = /^\d+(\.\d+)?$/;
const ZERO_AMOUNT_PATTERN = /^0(\.0+)?$/;
const MISSING_PRICE_TABLE_REASON = "the model call was recorded without a price table";
const UNREADABLE_COST_REASON = "the recorded cost cannot be read";
const UNREADABLE_AUTHORIZATION = "unreadable";
const AUTHORIZATION_REASONS = ["rule", "no_rule", "unevaluable", "rejected"];

const statusLine = document.querySelector("#status");
const timeline = document.querySelector("#timeline");
const detail = document.querySelector("#detail");
const fileInput = document.querySelector("#file");
const scrubber = document.querySelector("#scrubber");
const scrubberPosition = document.querySelector("#scrubber-position");
const stackedLayout = window.matchMedia("(max-width: 60rem)");
let rows = [];

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

function hasTextFields(record, names) {
  return (
    record !== null &&
    typeof record === "object" &&
    names.every((name) => typeof record[name] === "string")
  );
}

function parseRunFile(text) {
  let file;
  try {
    file = JSON.parse(text);
  } catch {
    throw new Error("This file is not JSON.");
  }
  const isRunFile =
    file !== null &&
    typeof file === "object" &&
    file.format === RUN_FILE_FORMAT &&
    hasTextFields(file.run, ["run_id", "name", "status", "created_at"]) &&
    Array.isArray(file.attempts) &&
    file.attempts.every((attempt) =>
      hasTextFields(attempt, ["step_key", "name", "status", "started_at", "finished_at"]),
    );
  if (!isRunFile) {
    throw new Error(`This is not a run file in the ${RUN_FILE_FORMAT} format.`);
  }
  return file;
}

function secondsBetween(start, end) {
  return (Date.parse(end) - Date.parse(start)) / 1000;
}

function durationText(seconds) {
  if (Number.isNaN(seconds)) {
    return "not finished";
  }
  if (seconds < 0.001) {
    return "under 1 ms";
  }
  if (seconds < 1) {
    return `${Math.round(seconds * 1000)} ms`;
  }
  if (seconds < 60) {
    return `${seconds.toFixed(1)} s`;
  }
  return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
}

function decimalPlaces(amount) {
  const [, fraction = ""] = amount.split(".");
  return fraction.length;
}

function sumOfAmounts(amounts) {
  const places = Math.max(0, ...amounts.map(decimalPlaces));
  let scaledTotal = 0n;
  for (const amount of amounts) {
    const [whole, fraction = ""] = amount.split(".");
    scaledTotal += BigInt(whole + fraction.padEnd(places, "0"));
  }
  const digits = scaledTotal.toString().padStart(places + 1, "0");
  if (places === 0) {
    return digits;
  }
  return `${digits.slice(0, -places)}.${digits.slice(-places)}`;
}

function isModelCall(attempt) {
  return Object.keys(attempt.annotations ?? {}).some((key) => key.startsWith("gen_ai."));
}

function costFromRecord(recorded, usd, basis, reason) {
  return {
    usd,
    basis,
    reason,
    parts: recorded.parts ?? null,
    priceTable: recorded.price_table ?? null,
    priceTableHash: recorded.price_table_hash ?? null,
    model: recorded.model ?? null,
    variant: recorded.variant ?? null,
  };
}

function unknownCost(reason) {
  return costFromRecord({}, null, "unknown", reason);
}

function attemptCost(attempt) {
  const recorded = attempt.annotations?.["cuaderna.cost"];
  if (recorded === undefined) {
    if (isModelCall(attempt)) {
      return unknownCost(MISSING_PRICE_TABLE_REASON);
    }
    return null;
  }
  if (recorded === null || typeof recorded !== "object") {
    return unknownCost(UNREADABLE_COST_REASON);
  }
  if (recorded.usd === null) {
    const reason = typeof recorded.reason === "string" ? recorded.reason : UNREADABLE_COST_REASON;
    return costFromRecord(recorded, null, "unknown", reason);
  }
  if (typeof recorded.usd !== "string" || !AMOUNT_PATTERN.test(recorded.usd)) {
    return unknownCost(UNREADABLE_COST_REASON);
  }
  return costFromRecord(recorded, recorded.usd, recorded.basis ?? null, recorded.reason ?? null);
}

function runCost(attempts) {
  const costs = attempts.map(attemptCost).filter((cost) => cost !== null);
  if (costs.length === 0) {
    return null;
  }
  const knownAmounts = costs.filter((cost) => cost.usd !== null).map((cost) => cost.usd);
  return {
    total: sumOfAmounts(knownAmounts),
    unknownCount: costs.length - knownAmounts.length,
  };
}

function attemptCostText(cost) {
  if (cost === null) {
    return "";
  }
  if (cost.usd === null) {
    return "unknown cost";
  }
  return `${cost.usd} USD`;
}

function runCostText(cost) {
  if (cost === null) {
    return "";
  }
  return `${cost.total} USD`;
}

function priceTableText(cost) {
  if (cost.priceTable === null || cost.priceTableHash === null) {
    return cost.priceTable;
  }
  return `${cost.priceTable} (${cost.priceTableHash})`;
}

function costPartFacts(parts) {
  if (parts === null || typeof parts !== "object") {
    return [];
  }
  return COST_PART_LABELS.filter(
    ([, key]) => typeof parts[key] === "string" && !ZERO_AMOUNT_PATTERN.test(parts[key]),
  ).map(([label, key]) => [label, `${parts[key]} USD`]);
}

function costFacts(cost) {
  let variant = null;
  if (cost.variant !== null) {
    variant = JSON.stringify(cost.variant);
  }
  let amount = "unknown";
  if (cost.usd !== null) {
    amount = `${cost.usd} USD`;
  }
  const facts = [
    ["cost", amount],
    ["basis", cost.basis],
    ["why", cost.reason],
    ["price table", priceTableText(cost)],
    ["priced as", cost.model],
    ["variant", variant],
  ].filter(([, value]) => value !== null);
  return [...facts, ...costPartFacts(cost.parts)];
}

function recordedAuthorization(attempt) {
  const recorded = attempt.annotations?.["cuaderna.authorization"];
  if (recorded === undefined) {
    return null;
  }
  const isReadable =
    recorded !== null &&
    typeof recorded === "object" &&
    (recorded.decision === "allow" || recorded.decision === "deny") &&
    AUTHORIZATION_REASONS.includes(recorded.reason);
  if (!isReadable) {
    return { unreadable: true };
  }
  return recorded;
}

function textOrNull(value) {
  if (typeof value === "string") {
    return value;
  }
  return null;
}

function authorizationRuleIds(recorded) {
  if (!Array.isArray(recorded.rules)) {
    return [];
  }
  return recorded.rules.filter((rule) => typeof rule === "string");
}

function authorizationReasonText(recorded) {
  const ruleIds = authorizationRuleIds(recorded).join(", ");
  if (recorded.reason === "no_rule") {
    return "no rule names this step";
  }
  if (recorded.reason === "rejected") {
    return "a person rejected it";
  }
  if (recorded.reason === "unevaluable") {
    return ["a rule could not be evaluated", ruleIds].filter((part) => part !== "").join(": ");
  }
  if (ruleIds === "") {
    return "a rule, no rule ids recorded";
  }
  return ruleIds;
}

function authorizationPolicyText(recorded) {
  const policy = textOrNull(recorded.policy);
  const hash = textOrNull(recorded.policy_hash);
  if (policy === null || hash === null) {
    return policy ?? hash;
  }
  return `${policy} (${hash})`;
}

function authorizationApprovalFacts(recorded) {
  const approval = recorded.approval;
  if (approval === null || approval === undefined) {
    return [];
  }
  if (typeof approval !== "object") {
    return [["approval", UNREADABLE_AUTHORIZATION]];
  }
  let verb = "approved by";
  if (recorded.decision === "deny") {
    verb = "rejected by";
  }
  return [
    [verb, textOrNull(approval.by) ?? UNREADABLE_AUTHORIZATION],
    ["decided at", textOrNull(approval.at)],
    ["note", textOrNull(approval.note)],
  ];
}

function authorizationFacts(recorded) {
  if (recorded.unreadable) {
    return [["authorization", UNREADABLE_AUTHORIZATION]];
  }
  let policySaid = null;
  if (recorded.effect === "ask") {
    policySaid = "ask a person";
  }
  const facts = [
    ["decision", recorded.decision],
    ["policy said", policySaid],
    ["why", authorizationReasonText(recorded)],
    ["policy", authorizationPolicyText(recorded)],
    ["actor", textOrNull(recorded.actor)],
    ...authorizationApprovalFacts(recorded),
  ];
  return facts.filter(([, value]) => value !== null);
}

function authorizationLabel(recorded) {
  if (recorded === null || recorded.unreadable) {
    return null;
  }
  if (recorded.decision === "deny") {
    return "denied";
  }
  if (recorded.approval !== null && typeof recorded.approval === "object") {
    return "approved";
  }
  return null;
}

function rowStatusText(attempt) {
  const label = authorizationLabel(recordedAuthorization(attempt));
  if (label === "approved" && attempt.status !== "ok") {
    return attempt.status;
  }
  return label ?? attempt.status;
}

function leaseGroups(attempts) {
  const groups = [];
  attempts.forEach((attempt, index) => {
    const leaseToken = attempt.lease_token ?? null;
    const current = groups.at(-1);
    if (current && current.leaseToken === leaseToken) {
      current.rows.push({ attempt, index });
    } else {
      groups.push({ leaseToken, rows: [{ attempt, index }] });
    }
  });
  return groups;
}

function valueNode(value) {
  if (typeof value === "string") {
    return element("pre", "", value);
  }
  if (value === null || typeof value !== "object") {
    return element("code", "", JSON.stringify(value));
  }
  const entries = Object.entries(value);
  if (entries.length === 0) {
    return element("code", "", JSON.stringify(value));
  }
  return factsNode(entries.map(([name, inner]) => [name, valueNode(inner)]));
}

function factsNode(facts) {
  const list = element("dl");
  for (const [name, content] of facts) {
    const description = element("dd");
    description.append(content);
    list.append(element("dt", "", name), description);
  }
  return list;
}

function sectionNodes(title, value) {
  return [element("h3", "", title), valueNode(value)];
}

function outcomeNodes(succeeded, failed, result, error) {
  if (succeeded) {
    return sectionNodes("Result", result);
  }
  if (failed) {
    return sectionNodes("Error", error);
  }
  return [];
}

function detailTitle(text) {
  const title = element("h2", "", text);
  title.id = "detail-title";
  return title;
}

function showRunDetail(run, attemptCount, cost) {
  const facts = [
    ["function", run.name],
    ["status", run.status],
    ["code version", run.code_version ?? "not recorded"],
    ["created", run.created_at],
    ["finished", run.finished_at ?? "not finished"],
    ["attempts recorded", String(attemptCount)],
  ];
  if (cost !== null) {
    facts.push(
      ["cost", runCostText(cost)],
      ["attempts of unknown cost", String(cost.unknownCount)],
    );
  }
  detail.replaceChildren(
    detailTitle(`Run ${run.run_id}`),
    factsNode(facts),
    ...sectionNodes("Input", run.input),
    ...outcomeNodes(run.status === "completed", run.status === "failed", run.output, run.error),
  );
}

function showAttemptDetail(attempt) {
  const annotations = attempt.annotations ?? {};
  const modelFacts = MODEL_FACTS.filter(([, key]) => key in annotations).map(([label, key]) => [
    label,
    String(annotations[key]),
  ]);
  const nodes = [
    detailTitle(attempt.step_key),
    factsNode([
      ["function", attempt.name],
      ["attempt", String(attempt.attempt)],
      ["status", attempt.status],
      ["started", attempt.started_at],
      ["finished", attempt.finished_at],
      ["duration", durationText(secondsBetween(attempt.started_at, attempt.finished_at))],
      ["lease token", String(attempt.lease_token ?? "not recorded")],
    ]),
  ];
  if (modelFacts.length > 0) {
    nodes.push(element("h3", "", "Model call"), factsNode(modelFacts));
  }
  const cost = attemptCost(attempt);
  if (cost !== null) {
    nodes.push(element("h3", "", "Cost"), factsNode(costFacts(cost)));
  }
  const authorization = recordedAuthorization(attempt);
  if (authorization !== null) {
    nodes.push(element("h3", "", "Authorization"), factsNode(authorizationFacts(authorization)));
  }
  nodes.push(
    ...sectionNodes("Arguments", attempt.args),
    ...outcomeNodes(
      attempt.status === "ok",
      attempt.status === "error",
      attempt.result,
      attempt.error,
    ),
  );
  if (attempt.annotations) {
    nodes.push(...sectionNodes("Annotations", attempt.annotations));
  }
  detail.replaceChildren(...nodes);
}

function rowButton(cells, status, show) {
  const button = element("button", `row ${status}`);
  button.type = "button";
  button.append(...cells);
  const position = rows.length;
  rows.push({ button, show });
  button.addEventListener("click", () => {
    selectRow(position);
    if (stackedLayout.matches) {
      detail.focus();
    }
  });
  return button;
}

function positionText(position) {
  if (position === 0) {
    return "the whole run";
  }
  return `attempt ${position} of ${rows.length - 1}`;
}

function selectRow(position) {
  rows.forEach(({ button }, index) => {
    button.classList.toggle("ahead", position > 0 && index > position);
    if (index === position) {
      button.setAttribute("aria-current", "true");
    } else {
      button.removeAttribute("aria-current");
    }
  });
  rows[position].show();
  scrubber.value = String(position);
  scrubberPosition.textContent = positionText(position);
}

function attemptRow(attempt, longestSeconds) {
  const seconds = secondsBetween(attempt.started_at, attempt.finished_at);
  const bar = element("span", "bar");
  bar.style.setProperty("--share", String(seconds / longestSeconds));
  const button = rowButton(
    [
      element("span", "key", attempt.step_key),
      element("span", "", `attempt ${attempt.attempt}`),
      element("span", "", durationText(seconds)),
      element("span", "", attemptCostText(attemptCost(attempt))),
      element("span", "status", rowStatusText(attempt)),
      bar,
    ],
    attempt.status,
    () => showAttemptDetail(attempt),
  );
  const item = element("li");
  item.append(button);
  return item;
}

function takeoverNote(previousGroup, group) {
  const stopped = previousGroup.rows.at(-1).attempt.finished_at;
  const continued = group.rows[0].attempt.started_at;
  const gap = durationText(secondsBetween(stopped, continued));
  return element(
    "li",
    "takeover",
    `The process holding the run stopped here or gave the run back. Another one continued it and started its first step ${gap} later. If a step was executing when the first one stopped, it left no record and starts again below.`,
  );
}

function groupItem(group, position, longestSeconds) {
  const item = element("li");
  const title = element("h3", "", `Process ${position}`);
  let lease = "lease not recorded";
  if (group.leaseToken !== null) {
    lease = `lease token ${group.leaseToken}`;
  }
  title.append(element("small", "", ` ${lease}, ${group.rows.length} attempts`));
  const rows = element("ol");
  rows.append(...group.rows.map(({ attempt }) => attemptRow(attempt, longestSeconds)));
  item.append(title, rows);
  return item;
}

function statusText(run, attemptCount, cost) {
  let text = `Showing run ${run.run_id}: ${attemptCount} attempts, ${run.status}`;
  if (cost !== null) {
    text += `, ${runCostText(cost)}`;
    if (cost.unknownCount > 0) {
      text += `, ${cost.unknownCount} of unknown cost`;
    }
  }
  return `${text}.`;
}

function showRunFile(file) {
  const { run, attempts } = file;
  rows = [];
  const durations = attempts.map((attempt) =>
    secondsBetween(attempt.started_at, attempt.finished_at),
  );
  const longestSeconds = Math.max(...durations, 0.001);
  const cost = runCost(attempts);
  const runButton = rowButton(
    [
      element("span", "key", run.name),
      element("span", "", "the run"),
      element("span", "", durationText(secondsBetween(run.created_at, run.finished_at ?? ""))),
      element("span", "", runCostText(cost)),
      element("span", "status", run.status),
    ],
    run.status,
    () => showRunDetail(run, attempts.length, cost),
  );
  const runItem = element("li");
  runItem.append(runButton);
  const items = [runItem];
  const groups = leaseGroups(attempts);
  groups.forEach((group, position) => {
    if (position > 0) {
      items.push(takeoverNote(groups[position - 1], group));
    }
    items.push(groupItem(group, position + 1, longestSeconds));
  });
  if (attempts.length === 0) {
    items.push(element("li", "takeover", "This run has no recorded attempts."));
  }
  timeline.replaceChildren(...items);
  scrubber.max = String(attempts.length);
  selectRow(0);
  statusLine.classList.remove("problem");
  statusLine.textContent = statusText(run, attempts.length, cost);
}

function showProblem(message) {
  statusLine.textContent = message;
  statusLine.classList.add("problem");
}

function showRunText(text) {
  try {
    showRunFile(parseRunFile(text));
  } catch (error) {
    showProblem(error.message);
  }
}

async function loadSample() {
  let response;
  try {
    response = await fetch(SAMPLE_URL);
  } catch {
    showProblem("The sample run could not be loaded.");
    return;
  }
  if (!response.ok) {
    showProblem(`The sample run could not be loaded (HTTP ${response.status}).`);
    return;
  }
  showRunText(await response.text());
}

async function openFile(file) {
  if (file) {
    showRunText(await file.text());
  }
}

fileInput.addEventListener("change", () => openFile(fileInput.files[0]));
document.addEventListener("dragover", (event) => event.preventDefault());
document.addEventListener("drop", (event) => {
  event.preventDefault();
  openFile(event.dataTransfer.files[0]);
});
scrubber.addEventListener("input", () => {
  const position = Number(scrubber.value);
  selectRow(position);
  rows[position].button.scrollIntoView({ block: "nearest" });
});

loadSample();
