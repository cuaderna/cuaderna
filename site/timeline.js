const RUN_FILE_FORMAT = "cuaderna.run/1";
const SAMPLE_URL = "sample-run.json";

const statusLine = document.querySelector("#status");
const timeline = document.querySelector("#timeline");

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
    file.run !== null &&
    typeof file.run === "object" &&
    Array.isArray(file.attempts);
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

function rowItem(cells, status) {
  const item = element("li", `row ${status}`);
  item.append(...cells);
  return item;
}

function attemptRow(attempt, longestSeconds) {
  const seconds = secondsBetween(attempt.started_at, attempt.finished_at);
  const bar = element("span", "bar");
  bar.style.setProperty("--share", String(seconds / longestSeconds));
  return rowItem(
    [
      element("span", "key", attempt.step_key),
      element("span", "", `attempt ${attempt.attempt}`),
      element("span", "", durationText(seconds)),
      element("span", "status", attempt.status),
      bar,
    ],
    attempt.status,
  );
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
  const durations = attempts.map((attempt) =>
    secondsBetween(attempt.started_at, attempt.finished_at),
  );
  const longestSeconds = Math.max(...durations, 0.001);
  const runItem = rowItem(
    [
      element("span", "key", run.name),
      element("span", "", "the run"),
      element("span", "", durationText(secondsBetween(run.created_at, run.finished_at ?? ""))),
      element("span", "status", run.status),
    ],
    run.status,
  );
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
  statusLine.textContent = `Showing run ${run.run_id}: ${attempts.length} attempts, ${run.status}.`;
}

function showProblem(message) {
  statusLine.textContent = message;
  statusLine.classList.add("problem");
}

async function loadSample() {
  try {
    const response = await fetch(SAMPLE_URL);
    if (!response.ok) {
      throw new Error(`The sample run could not be loaded (HTTP ${response.status}).`);
    }
    showRunFile(parseRunFile(await response.text()));
  } catch (error) {
    if (error instanceof TypeError) {
      showProblem("The sample run could not be loaded.");
    } else {
      showProblem(error.message);
    }
  }
}

loadSample();
