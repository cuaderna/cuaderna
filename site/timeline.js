const SAMPLE_URL = "sample-run.json";
const INDEX_URL = "runs.json";
const MODEL_FACTS = [
  ["requested model", "gen_ai.request.model"],
  ["answered by", "gen_ai.response.model"],
  ["input tokens", "gen_ai.usage.input_tokens"],
  ["input tokens read from cache", "gen_ai.usage.cache_read.input_tokens"],
  ["output tokens", "gen_ai.usage.output_tokens"],
];
const ZERO_AMOUNT_PATTERN = /^0(\.0+)?$/;
const MISSING_PRICE_TABLE_REASON = "the model call was recorded without a price table";
const UNREADABLE_COST_REASON = "the recorded cost cannot be read";
const UNREADABLE_AUTHORIZATION = "unreadable";
const NESTED_TOO_DEEPLY = "nested too deeply to show";
const ATTEMPT_CANNOT_BE_SHOWN = "This attempt cannot be shown.";
const FILE_CANNOT_BE_SHOWN = "This file could not be shown.";
const FILE_CANNOT_BE_READ = "This file could not be read.";
const FILE_IS_NOT_TEXT = "This file is not UTF-8 text.";
const SAMPLE_RUN = "The sample run";
const PICKED_RUN = "That run";

const labels = globalThis.cuadernaLabels;
const statusLine = document.querySelector("#status");
const timeline = document.querySelector("#timeline");
const detail = document.querySelector("#detail");
const fileInput = document.querySelector("#file");
const scrubber = document.querySelector("#scrubber");
const scrubberPosition = document.querySelector("#scrubber-position");
const picker = document.querySelector("#run-picker");
const pickerLabel = document.querySelector("#run-picker-label");
const experimentNote = document.querySelector("#experiment-note");
const stackedLayout = window.matchMedia("(max-width: 60rem)");
let rows = [];
let latestRequest = 0;

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

function isolated(text) {
  const node = document.createElement("bdi");
  node.dir = "auto";
  node.textContent = text;
  return node;
}

function cell(className, text) {
  const node = element("span", className);
  node.append(isolated(text));
  return node;
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
    partsAgree: usd === null || labels.costPartsAgree(usd, recorded.parts ?? null),
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
  if (!labels.isAmount(recorded.usd)) {
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
    total: labels.sumOfAmounts(knownAmounts),
    knownCount: knownAmounts.length,
    unknownCount: costs.length - knownAmounts.length,
    disagreeingCount: costs.filter((cost) => !cost.partsAgree).length,
  };
}

function attemptCostText(cost) {
  if (cost === null) {
    return "";
  }
  return labels.costText(cost.usd);
}

function runCostText(cost) {
  if (cost === null) {
    return "";
  }
  if (cost.knownCount === 0) {
    return "unknown";
  }
  if (cost.unknownCount > 0) {
    return `${cost.total} USD + ${cost.unknownCount} unknown`;
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
  return labels.COST_PART_LABELS.filter(
    ([, key]) => typeof parts[key] === "string" && !ZERO_AMOUNT_PATTERN.test(parts[key]),
  ).map(([label, key]) => [label, `${parts[key]} USD`]);
}

function costFacts(cost) {
  let variant = null;
  if (cost.variant !== null) {
    variant = JSON.stringify(cost.variant);
  }
  let check = null;
  if (!cost.partsAgree) {
    check = "the parts do not add up to the cost";
  }
  const facts = [
    ["cost", labels.costText(cost.usd)],
    ["check", check],
    ["basis", cost.basis],
    ["why", cost.reason],
    ["price table", priceTableText(cost)],
    ["priced as", cost.model],
    ["variant", variant],
  ].filter(([, value]) => value !== null);
  return [...facts, ...costPartFacts(cost.parts)];
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
  if (recorded === null) {
    return [["authorization", "not recorded"]];
  }
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

function faultFacts(recorded) {
  if (recorded.unreadable) {
    return [["injected fault", UNREADABLE_AUTHORIZATION]];
  }
  let stepRan = "no";
  if (recorded.executed) {
    stepRan = "yes";
  }
  return [
    ["fault", recorded.fault_id],
    ["kind", recorded.kind],
    ["step ran", stepRan],
  ];
}

function reuseFacts(recorded) {
  if (recorded.unreadable) {
    return [["from the recording", UNREADABLE_AUTHORIZATION]];
  }
  return [
    ["answer taken from run", recorded.run_id],
    ["step", recorded.step_key],
    ["attempt", String(recorded.attempt)],
    ["step ran", "no, the recorded answer was used"],
  ];
}

function rowStatusText(attempt) {
  const label = labels.authorizationLabel(labels.recordedAuthorization(attempt));
  const showsStatus = label === "allowed" || (label === "approved" && attempt.status !== "ok");
  if (showsStatus) {
    return labels.statusLabel(attempt.status);
  }
  return label;
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

function valueNode(value, depth = 0) {
  if (typeof value === "string") {
    const pre = element("pre");
    pre.append(isolated(value));
    return pre;
  }
  if (value === null || typeof value !== "object") {
    const code = element("code");
    code.append(isolated(JSON.stringify(value)));
    return code;
  }
  if (depth >= labels.MAX_VALUE_DEPTH) {
    return element("em", "", NESTED_TOO_DEEPLY);
  }
  const entries = Object.entries(value);
  if (entries.length === 0) {
    const code = element("code");
    code.append(isolated(JSON.stringify(value)));
    return code;
  }
  return factsNode(entries.map(([name, inner]) => [name, valueNode(inner, depth + 1)]));
}

function factsNode(facts) {
  const list = element("dl");
  for (const [name, content] of facts) {
    const term = element("dt");
    term.append(isolated(name));
    const description = element("dd");
    if (typeof content === "string") {
      description.append(isolated(content));
    } else {
      description.append(content);
    }
    list.append(term, description);
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

function detailTitle(...parts) {
  const title = element("h2");
  title.append(...parts);
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
    if (cost.disagreeingCount > 0) {
      facts.push(["attempts whose parts do not add up", String(cost.disagreeingCount)]);
    }
  }
  detail.replaceChildren(
    detailTitle("Run ", isolated(run.run_id)),
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
    detailTitle(isolated(attempt.step_key)),
    factsNode([
      ["function", attempt.name],
      ["attempt", String(attempt.attempt)],
      ["status", attempt.status],
      ["started", attempt.started_at],
      ["finished", attempt.finished_at],
      ["duration", labels.durationText(attempt.started_at, attempt.finished_at)],
      ["lease token", String(attempt.lease_token ?? "not recorded")],
      ["chain hash", labels.chainText(attempt.chain_hash)],
    ]),
  ];
  if (modelFacts.length > 0) {
    nodes.push(element("h3", "", "Model call"), factsNode(modelFacts));
  }
  const cost = attemptCost(attempt);
  if (cost !== null) {
    nodes.push(element("h3", "", "Cost"), factsNode(costFacts(cost)));
  }
  nodes.push(
    element("h3", "", "Authorization"),
    factsNode(authorizationFacts(labels.recordedAuthorization(attempt))),
  );
  const fault = labels.recordedFault(attempt);
  if (fault !== null) {
    nodes.push(element("h3", "", "Injected fault"), factsNode(faultFacts(fault)));
  }
  const reuse = labels.recordedReuse(attempt);
  if (reuse !== null) {
    nodes.push(element("h3", "", "From the recording"), factsNode(reuseFacts(reuse)));
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
  const button = element("button", `row ${labels.statusClass(status)}`);
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
  try {
    rows[position].show();
  } catch {
    detail.replaceChildren(element("p", "", ATTEMPT_CANNOT_BE_SHOWN));
  }
  scrubber.value = String(position);
  scrubberPosition.textContent = positionText(position);
}

function attemptRow(attempt, longestSeconds) {
  const seconds = labels.validSeconds(attempt.started_at, attempt.finished_at) ?? 0;
  const bar = element("span", "bar");
  bar.style.setProperty("--share", String(seconds / longestSeconds));
  const button = rowButton(
    [
      cell("key", attempt.step_key),
      cell("", `attempt ${attempt.attempt}`),
      cell("", labels.durationText(attempt.started_at, attempt.finished_at)),
      cell("", attemptCostText(attemptCost(attempt))),
      cell("status", [rowStatusText(attempt), ...labels.attemptMarks(attempt)].join(", ")),
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
  const gap = labels.durationText(stopped, continued);
  let timing = `started its first step ${gap} later`;
  if (gap === "invalid time") {
    timing = "started at a recorded time that is invalid";
  }
  return element(
    "li",
    "takeover",
    `The process holding the run stopped here or gave the run back. Another one continued it and ${timing}. If a step was executing when the first one stopped, it left no record and starts again below.`,
  );
}

function groupItem(group, position, longestSeconds) {
  const item = element("li");
  const title = element("h3", "", `Process ${position}`);
  const small = element("small", "", " ");
  if (group.leaseToken === null) {
    small.append("lease not recorded");
  } else {
    small.append("lease token ", isolated(String(group.leaseToken)));
  }
  small.append(`, ${group.rows.length} attempts`);
  title.append(small);
  const rows = element("ol");
  rows.append(...group.rows.map(({ attempt }) => attemptRow(attempt, longestSeconds)));
  item.append(title, rows);
  return item;
}

function statusNodes(run, attemptCount, cost) {
  const nodes = [
    "Showing run ",
    isolated(run.run_id),
    `: ${attemptCount} attempts, `,
    isolated(labels.statusLabel(run.status)),
  ];
  if (cost !== null) {
    nodes.push(`, ${runCostText(cost)}`);
    if (cost.disagreeingCount > 0) {
      nodes.push(`, ${cost.disagreeingCount} with parts that do not add up`);
    }
  }
  return [...nodes, "."];
}

function showExperimentNote(line) {
  if (line === null) {
    experimentNote.replaceChildren();
    experimentNote.hidden = true;
    return;
  }
  experimentNote.textContent = line;
  experimentNote.hidden = false;
}

function showRunFile(file) {
  const { run, attempts } = file;
  const experimentLine = labels.experimentLine(labels.experimentFacts(file));
  const previousRows = rows;
  rows = [];
  try {
    buildTimeline(run, attempts);
  } catch (error) {
    rows = previousRows;
    throw error;
  }
  scrubber.max = String(attempts.length);
  selectRow(0);
  showExperimentNote(experimentLine);
  statusLine.classList.remove("problem");
  statusLine.replaceChildren(...statusNodes(run, attempts.length, runCost(attempts)));
}

function buildTimeline(run, attempts) {
  const longestSeconds = attempts.reduce(
    (longest, attempt) =>
      Math.max(longest, labels.validSeconds(attempt.started_at, attempt.finished_at) ?? 0),
    0.001,
  );
  const cost = runCost(attempts);
  const runButton = rowButton(
    [
      cell("key", run.name),
      cell("", "the run"),
      cell("", labels.durationText(run.created_at, run.finished_at ?? "")),
      cell("", runCostText(cost)),
      cell("status", labels.statusLabel(run.status)),
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
}

function showProblem(message) {
  statusLine.textContent = message;
  statusLine.classList.add("problem");
}

function showRunText(text) {
  let file;
  try {
    file = labels.parseRunFile(text);
  } catch (error) {
    showProblem(error.message);
    return;
  }
  try {
    showRunFile(file);
  } catch {
    showProblem(FILE_CANNOT_BE_SHOWN);
  }
}

async function fetchedText(url, subject) {
  let response;
  try {
    response = await fetch(url);
  } catch {
    throw new Error(`${subject} could not be loaded.`);
  }
  if (!response.ok) {
    throw new Error(`${subject} could not be loaded (HTTP ${response.status}).`);
  }
  const text = await response.text();
  const sizeProblem = labels.fileSizeProblem(new Blob([text]).size);
  if (sizeProblem !== null) {
    throw new Error(sizeProblem);
  }
  return text;
}

function fillPicker(choices) {
  const options = choices.map(({ file, label }) => {
    const option = element("option", "", label);
    option.value = file;
    return option;
  });
  picker.replaceChildren(...options);
  pickerLabel.hidden = false;
}

async function loadIndex() {
  try {
    const choices = labels.pickerChoices(
      labels.parseRunIndex(await fetchedText(INDEX_URL, "The run index")),
    );
    if (choices.length > 1) {
      fillPicker(choices);
    }
  } catch {
    pickerLabel.hidden = true;
  }
}

async function fileText(file) {
  const sizeProblem = labels.fileSizeProblem(file.size);
  if (sizeProblem !== null) {
    throw new Error(sizeProblem);
  }
  let bytes;
  try {
    bytes = await file.arrayBuffer();
  } catch {
    throw new Error(FILE_CANNOT_BE_READ);
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new Error(FILE_IS_NOT_TEXT);
  }
}

async function showLatest(readText) {
  latestRequest += 1;
  const request = latestRequest;
  let text;
  try {
    text = await readText();
  } catch (error) {
    if (request === latestRequest) {
      showProblem(error.message);
    }
    return;
  }
  if (request === latestRequest) {
    showRunText(text);
  }
}

function openFile(file) {
  if (file) {
    showLatest(() => fileText(file));
  }
}

fileInput.addEventListener("change", () => openFile(fileInput.files[0]));
document.addEventListener("dragover", (event) => event.preventDefault());
document.addEventListener("drop", (event) => {
  event.preventDefault();
  openFile(event.dataTransfer.files[0]);
});
picker.addEventListener("change", () =>
  showLatest(() => fetchedText(picker.value, PICKED_RUN)),
);
scrubber.addEventListener("input", () => {
  const position = Number(scrubber.value);
  selectRow(position);
  rows[position].button.scrollIntoView({ block: "nearest" });
});

showLatest(() => fetchedText(SAMPLE_URL, SAMPLE_RUN));
loadIndex();