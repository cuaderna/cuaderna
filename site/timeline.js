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
const summary = document.querySelector("#summary");
const summaryFacts = document.querySelector("#summary-facts");
const handovers = document.querySelector("#handovers");
const stackedLayout = window.matchMedia("(max-width: 60rem)");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
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

function capitalized(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
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
  let reasonLabel = "why";
  if (recorded.decision === "allow" && recorded.reason === "rule") {
    reasonLabel = "allowed by";
  }
  const facts = [
    ["decision", recorded.decision],
    ["policy said", policySaid],
    [reasonLabel, authorizationReasonText(recorded)],
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

function factsNode(facts, isHumanLabeled = false) {
  let className = "";
  if (isHumanLabeled) {
    className = "facts";
  }
  const list = element("dl", className);
  for (const [name, content] of facts) {
    const term = element("dt");
    if (isHumanLabeled) {
      term.textContent = capitalized(name);
    } else {
      term.append(isolated(name));
    }
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

function readableFacts(facts) {
  return factsNode(facts, true);
}

function timeNode(time) {
  const node = element("span");
  if (typeof time === "string") {
    node.title = time;
  }
  node.append(isolated(labels.timeText(time)));
  return node;
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

function runFinishedFact(run) {
  if (run.finished_at === null || run.finished_at === undefined) {
    return "not finished";
  }
  return timeNode(run.finished_at);
}

function showRunDetail(run, attemptCount, cost) {
  const facts = [
    ["function", run.name],
    ["status", run.status],
    ["code version", run.code_version ?? "not recorded"],
    ["created", timeNode(run.created_at)],
    ["finished", runFinishedFact(run)],
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
    detailTitle("Whole run ", isolated(run.run_id)),
    readableFacts(facts),
    ...sectionNodes("Input", run.input),
    ...outcomeNodes(run.status === "completed", run.status === "failed", run.output, run.error),
  );
}

function processText(process) {
  if (process.leaseToken === null) {
    return `process ${process.number}, lease token not recorded`;
  }
  return `process ${process.number}, lease token ${process.leaseToken}`;
}

function showAttemptDetail(attempt, process) {
  const annotations = attempt.annotations ?? {};
  const modelFacts = MODEL_FACTS.filter(([, key]) => key in annotations).map(([label, key]) => [
    label,
    String(annotations[key]),
  ]);
  const facts = [
    ["function", attempt.name],
    ["status", attempt.status],
    ["retry", labels.retryText(attempt.attempt)],
    ["started", timeNode(attempt.started_at)],
    ["finished", timeNode(attempt.finished_at)],
    ["took", labels.durationText(attempt.started_at, attempt.finished_at)],
    ["written by", processText(process)],
    ["chain hash", labels.chainText(attempt.chain_hash)],
  ].filter(([, value]) => value !== null);
  const nodes = [detailTitle(isolated(attempt.step_key)), readableFacts(facts)];
  if (modelFacts.length > 0) {
    nodes.push(element("h3", "", "Model call"), readableFacts(modelFacts));
  }
  const cost = attemptCost(attempt);
  if (cost !== null) {
    nodes.push(element("h3", "", "Cost"), readableFacts(costFacts(cost)));
  }
  nodes.push(
    element("h3", "", "Permission"),
    readableFacts(authorizationFacts(labels.recordedAuthorization(attempt))),
  );
  const fault = labels.recordedFault(attempt);
  if (fault !== null) {
    nodes.push(element("h3", "", "Injected fault"), readableFacts(faultFacts(fault)));
  }
  const reuse = labels.recordedReuse(attempt);
  if (reuse !== null) {
    nodes.push(element("h3", "", "From the recording"), readableFacts(reuseFacts(reuse)));
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
    nodes.push(...sectionNodes("Everything else recorded", attempt.annotations));
  }
  detail.replaceChildren(...nodes);
}

function scrollBehavior() {
  if (reducedMotion.matches) {
    return "auto";
  }
  return "smooth";
}

function bringDetailIntoView() {
  const heading = detail.querySelector("#detail-title");
  detail.scrollIntoView({ behavior: scrollBehavior(), block: "start" });
  if (heading === null) {
    detail.focus({ preventScroll: true });
    return;
  }
  heading.tabIndex = -1;
  heading.focus({ preventScroll: true });
}

function backToStepsButton(position) {
  const button = element("button", "back-to-steps", "Back to the steps");
  button.type = "button";
  button.addEventListener("click", () => {
    const { button: row } = rows[position];
    row.scrollIntoView({ behavior: scrollBehavior(), block: "center" });
    row.focus({ preventScroll: true });
  });
  return button;
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
      bringDetailIntoView();
    }
  });
  return button;
}

function positionText(position) {
  if (position === 0) {
    return "the whole run";
  }
  return `step ${position} of ${rows.length - 1}`;
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
  detail.prepend(backToStepsButton(position));
  scrubber.value = String(position);
  scrubberPosition.textContent = positionText(position);
}

function metaNode(parts, marks) {
  const meta = element("span", "meta");
  for (const text of parts.filter((part) => part !== "")) {
    meta.append(cell("", text));
  }
  for (const text of marks) {
    meta.append(cell("mark", text));
  }
  return meta;
}

function attemptRow(attempt, longestSeconds, process) {
  const seconds = labels.validSeconds(attempt.started_at, attempt.finished_at) ?? 0;
  const bar = element("span", "bar");
  bar.setAttribute("aria-hidden", "true");
  bar.style.setProperty("--share", String(seconds / longestSeconds));
  const button = rowButton(
    [
      cell("key", attempt.step_key),
      cell("status", rowStatusText(attempt)),
      metaNode(
        [
          labels.durationText(attempt.started_at, attempt.finished_at),
          attemptCostText(attemptCost(attempt)),
          labels.retryText(attempt.attempt) ?? "",
        ],
        labels.attemptMarks(attempt),
      ),
      bar,
    ],
    attempt.status,
    () => showAttemptDetail(attempt, process),
  );
  const item = element("li");
  item.append(button);
  return item;
}

function gapBetween(previousGroup, group) {
  const lastAttempt = previousGroup.rows.at(-1).attempt;
  return labels.durationText(lastAttempt.finished_at, group.rows[0].attempt.started_at);
}

function handoverSentence(previousGroup, group, processNumber) {
  const stoppedAfter = previousGroup.rows.at(-1).attempt.step_key;
  return labels.handoverText(processNumber, stoppedAfter, gapBetween(previousGroup, group));
}

function handoverItem(sentence) {
  const item = element("li", "handover");
  item.append(isolated(sentence));
  return item;
}

function groupItem(group, number, longestSeconds) {
  const item = element("li");
  const title = element("h3", "", `Process ${number}`);
  const attempts = group.rows.map(({ attempt }) => attempt);
  title.append(element("small", "", ` ${labels.stepsText(attempts)}`));
  const process = { number, leaseToken: group.leaseToken };
  const rowList = element("ol");
  rowList.append(...attempts.map((attempt) => attemptRow(attempt, longestSeconds, process)));
  item.append(title, rowList);
  return item;
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

function totalCostText(cost) {
  if (cost === null) {
    return "none recorded";
  }
  if (cost.disagreeingCount > 0) {
    return `${runCostText(cost)}, ${cost.disagreeingCount} with parts that do not add up`;
  }
  return runCostText(cost);
}

function processCountText(groups) {
  if (groups.length === 1) {
    return "1 process";
  }
  return `${groups.length} processes`;
}

function showSummary(run, attempts, groups, cost) {
  const facts = [
    ["outcome", labels.outcomeText(run)],
    ["steps", labels.stepsText(attempts)],
    ["written by", processCountText(groups)],
    ["total time", labels.durationText(run.created_at, run.finished_at ?? "")],
    ["total cost", totalCostText(cost)],
  ];
  summaryFacts.replaceChildren(...readableFacts(facts).children);
  const sentences = groups
    .slice(1)
    .map((group, index) => handoverSentence(groups[index], group, index + 2));
  if (sentences.length > 0) {
    sentences[sentences.length - 1] += ` ${labels.UNRECORDED_STEP_SENTENCE}`;
  }
  handovers.replaceChildren(...sentences.map(handoverItem));
  summary.hidden = false;
}

function showRunFile(file) {
  const { run, attempts } = file;
  const experimentLine = labels.experimentLine(labels.experimentFacts(file));
  const groups = leaseGroups(attempts);
  const cost = runCost(attempts);
  const previousRows = rows;
  rows = [];
  try {
    buildTimeline(run, attempts, groups, cost);
  } catch (error) {
    rows = previousRows;
    throw error;
  }
  scrubber.max = String(attempts.length);
  selectRow(0);
  showSummary(run, attempts, groups, cost);
  showExperimentNote(experimentLine);
  statusLine.classList.remove("problem");
  statusLine.replaceChildren("Showing run ", isolated(run.run_id), ".");
}

function buildTimeline(run, attempts, groups, cost) {
  const longestSeconds = attempts.reduce(
    (longest, attempt) =>
      Math.max(longest, labels.validSeconds(attempt.started_at, attempt.finished_at) ?? 0),
    0.001,
  );
  const runButton = rowButton(
    [
      cell("key", run.name),
      cell("status", labels.statusLabel(run.status)),
      metaNode(
        ["the whole run", labels.durationText(run.created_at, run.finished_at ?? ""), runCostText(cost)],
        [],
      ),
    ],
    run.status,
    () => showRunDetail(run, attempts.length, cost),
  );
  const runItem = element("li");
  runItem.append(runButton);
  const items = [runItem];
  groups.forEach((group, position) => {
    if (position > 0) {
      const gap = gapBetween(groups[position - 1], group);
      items.push(handoverItem(labels.takeoverText(position + 1, gap)));
    }
    items.push(groupItem(group, position + 1, longestSeconds));
  });
  if (attempts.length === 0) {
    items.push(element("li", "handover", "This run has no recorded attempts."));
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
  const groups = new Map();
  for (const { file, group, label } of choices) {
    const option = element("option", "", label);
    option.value = file;
    if (!groups.has(group)) {
      const optgroup = element("optgroup");
      optgroup.label = group;
      groups.set(group, optgroup);
    }
    groups.get(group).append(option);
  }
  picker.replaceChildren(...groups.values());
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