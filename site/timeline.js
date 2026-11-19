const RUN_FILE_FORMAT = "cuaderna.run/1";
const SAMPLE_URL = "sample-run.json";
const MODEL_FACTS = [
  ["requested model", "gen_ai.request.model"],
  ["answered by", "gen_ai.response.model"],
  ["input tokens", "gen_ai.usage.input_tokens"],
  ["input tokens read from cache", "gen_ai.usage.cache_read.input_tokens"],
  ["output tokens", "gen_ai.usage.output_tokens"],
];

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

function showRunDetail(run, attemptCount) {
  detail.replaceChildren(
    detailTitle(`Run ${run.run_id}`),
    factsNode([
      ["function", run.name],
      ["status", run.status],
      ["code version", run.code_version ?? "not recorded"],
      ["created", run.created_at],
      ["finished", run.finished_at ?? "not finished"],
      ["attempts recorded", String(attemptCount)],
    ]),
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
      element("span", "status", attempt.status),
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
    `The process holding the run stopped here. Another one took the run over and started its first step ${gap} later. A step that was executing when the first process stopped left no record, so it starts again below.`,
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

function showRunFile(file) {
  const { run, attempts } = file;
  rows = [];
  const durations = attempts.map((attempt) =>
    secondsBetween(attempt.started_at, attempt.finished_at),
  );
  const longestSeconds = Math.max(...durations, 0.001);
  const runButton = rowButton(
    [
      element("span", "key", run.name),
      element("span", "", "the run"),
      element("span", "", durationText(secondsBetween(run.created_at, run.finished_at ?? ""))),
      element("span", "status", run.status),
    ],
    run.status,
    () => showRunDetail(run, attempts.length),
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
  statusLine.textContent = `Showing run ${run.run_id}: ${attempts.length} attempts, ${run.status}.`;
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
