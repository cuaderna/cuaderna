(() => {
  const STATUS_CLASSES = {
    ok: "ok",
    error: "error",
    running: "running",
    completed: "completed",
    failed: "failed",
    imported_unfinished: "imported_unfinished",
  };
  const RUN_FILE_FORMATS = ["cuaderna.run/1", "cuaderna.run/2"];
  const MAX_FILE_BYTES = 16 * 1024 * 1024;
  const MAX_VALUE_DEPTH = 64;
  const AMOUNT_PATTERN = /^\d{1,30}(\.\d{1,30})?$/;
  const TIME_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
  const AUTHORIZATION_REASONS = ["rule", "no_rule", "unevaluable", "rejected"];
  const FAULT_KINDS = ["raise", "return", "append"];
  const MAX_INDEX_ENTRIES = 200;
  const MAX_INDEX_TEXT_LENGTH = 200;
  const INDEX_KINDS = ["source", "control", "experiment"];
  const INDEX_KEYS = ["file", "run_id", "kind", "fault", "trial", "ended", "outcome"];
  const INDEX_FILE_PATTERN = /^[A-Za-z0-9._-]+(\/[A-Za-z0-9._-]+)*\.json$/;
  const INDEX_RANKS = { source: 0, control: 1, experiment: 2 };
  const COST_PART_LABELS = [
    ["input", "input"],
    ["cache read", "cache_read"],
    ["cache write", "cache_write"],
    ["output", "output"],
    ["requests", "requests"],
  ];

  function isKnownStatus(status) {
    return Object.hasOwn(STATUS_CLASSES, status);
  }

  function statusClass(status) {
    if (isKnownStatus(status)) {
      return STATUS_CLASSES[status];
    }
    return "unknown";
  }

  function statusLabel(status) {
    if (isKnownStatus(status)) {
      return status;
    }
    return "unknown status";
  }

  function isAmount(value) {
    return typeof value === "string" && AMOUNT_PATTERN.test(value);
  }

  function costText(usd) {
    if (isAmount(usd)) {
      return `${usd} USD`;
    }
    return "unknown";
  }

  function decimalPlaces(amount) {
    const [, fraction = ""] = amount.split(".");
    return fraction.length;
  }

  function sumOfAmounts(amounts) {
    const places = amounts.reduce((longest, amount) => Math.max(longest, decimalPlaces(amount)), 0);
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

  function normalizedAmount(amount) {
    const digits = sumOfAmounts([amount]);
    if (!digits.includes(".")) {
      return digits;
    }
    return digits.replace(/0+$/, "").replace(/\.$/, "");
  }

  function costPartsAgree(usd, parts) {
    if (parts === null || typeof parts !== "object") {
      return true;
    }
    const present = COST_PART_LABELS.map(([, key]) => parts[key]).filter((part) => part !== undefined);
    if (!present.every(isAmount)) {
      return false;
    }
    return normalizedAmount(usd) === normalizedAmount(sumOfAmounts(["0", ...present]));
  }

  function validSeconds(start, end) {
    if (![start, end].every((time) => typeof time === "string" && TIME_PATTERN.test(time))) {
      return null;
    }
    const seconds = (Date.parse(end) - Date.parse(start)) / 1000;
    if (!Number.isFinite(seconds) || seconds < 0) {
      return null;
    }
    return seconds;
  }

  function durationText(start, end) {
    if (end === "") {
      return "not finished";
    }
    const seconds = validSeconds(start, end);
    if (seconds === null) {
      return "invalid time";
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

  function hasTextFields(record, names) {
    return (
      record !== null &&
      typeof record === "object" &&
      names.every((name) => typeof record[name] === "string")
    );
  }

  function hasObjectOrNoAnnotations(attempt) {
    const annotations = attempt.annotations;
    return (
      annotations === undefined ||
      annotations === null ||
      (typeof annotations === "object" && !Array.isArray(annotations))
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
      RUN_FILE_FORMATS.includes(file.format) &&
      hasTextFields(file.run, ["run_id", "name", "status", "created_at"]) &&
      Array.isArray(file.attempts) &&
      file.attempts.every(
        (attempt) =>
          hasTextFields(attempt, ["step_key", "name", "status", "started_at", "finished_at"]) &&
          hasObjectOrNoAnnotations(attempt),
      );
    if (!isRunFile) {
      throw new Error(`This is not a run file in the ${RUN_FILE_FORMATS.join(" or ")} format.`);
    }
    return file;
  }

  function fileSizeProblem(bytes) {
    if (bytes <= MAX_FILE_BYTES) {
      return null;
    }
    return `This file is larger than the ${MAX_FILE_BYTES / 1024 / 1024} MiB the page opens.`;
  }

  function chainText(chainHash) {
    if (typeof chainHash === "string" && chainHash !== "") {
      return "present, not checked here";
    }
    return "not recorded";
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

  function hasApprovalFields(approval) {
    return (
      typeof approval === "object" &&
      typeof approval.by === "string" &&
      approval.by !== "" &&
      typeof approval.at === "string" &&
      approval.at !== ""
    );
  }

  function authorizationLabel(recorded) {
    if (recorded === null) {
      return "not recorded";
    }
    if (recorded.unreadable) {
      return "unreadable";
    }
    if (recorded.decision === "deny") {
      return "denied";
    }
    const approval = recorded.approval ?? null;
    if (approval === null && recorded.effect === "ask") {
      return "no approval recorded";
    }
    if (approval === null) {
      return "allowed";
    }
    if (hasApprovalFields(approval)) {
      return "approved";
    }
    return "unreadable";
  }

  function isText(value) {
    return typeof value === "string";
  }

  function isTextOrNull(value) {
    return value === null || isText(value);
  }

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function recordedFault(attempt) {
    const recorded = attempt.annotations?.["cuaderna.fault"];
    if (recorded === undefined) {
      return null;
    }
    const isReadable =
      isObject(recorded) &&
      FAULT_KINDS.includes(recorded.kind) &&
      isText(recorded.fault_id) &&
      typeof recorded.executed === "boolean";
    if (!isReadable) {
      return { unreadable: true };
    }
    return recorded;
  }

  function recordedReuse(attempt) {
    const recorded = attempt.annotations?.["cuaderna.reuse"];
    if (recorded === undefined) {
      return null;
    }
    const isReadable =
      isObject(recorded) &&
      isText(recorded.run_id) &&
      isText(recorded.step_key) &&
      Number.isInteger(recorded.attempt);
    if (!isReadable) {
      return { unreadable: true };
    }
    return recorded;
  }

  function attemptMarks(attempt) {
    const marks = [];
    if (recordedFault(attempt) !== null) {
      marks.push("injected");
    }
    if (recordedReuse(attempt) !== null) {
      marks.push("from the recording");
    }
    return marks;
  }

  function recordedExperiment(run) {
    const recorded = run.experiment;
    if (recorded === undefined || recorded === null) {
      return null;
    }
    const isReadable =
      isObject(recorded) &&
      isText(recorded.source_run_id) &&
      Array.isArray(recorded.reuse) &&
      recorded.reuse.every(isText) &&
      isTextOrNull(recorded.plan_id) &&
      isTextOrNull(recorded.plan_hash) &&
      isTextOrNull(recorded.fault_id);
    if (!isReadable) {
      return { unreadable: true };
    }
    return recorded;
  }

  function wasInjectedWithoutRunning(attempt) {
    const fault = recordedFault(attempt);
    return fault !== null && fault.executed === false;
  }

  function executedForReal(attempt) {
    if (recordedReuse(attempt) !== null || wasInjectedWithoutRunning(attempt)) {
      return false;
    }
    return authorizationLabel(recordedAuthorization(attempt)) !== "denied";
  }

  function experimentFacts(file) {
    const experiment = recordedExperiment(file.run);
    if (experiment === null || experiment.unreadable) {
      return experiment;
    }
    const faulted = file.attempts.find((attempt) => {
      const fault = recordedFault(attempt);
      return fault !== null && !fault.unreadable;
    });
    const executedAgain = file.attempts.find(
      (attempt) => experiment.reuse.includes(attempt.name) && executedForReal(attempt),
    );
    return {
      sourceRunId: experiment.source_run_id,
      faultId: experiment.fault_id,
      faultStep: faulted?.step_key ?? null,
      executedAgainStep: executedAgain?.step_key ?? null,
      executedAgainAttempt: executedAgain?.attempt ?? null,
    };
  }

  function faultSentence(facts) {
    if (facts.faultId === null) {
      return "Nothing was changed.";
    }
    if (facts.faultStep === null) {
      return `Fault ${facts.faultId} was planned but no step carries it.`;
    }
    return `Fault ${facts.faultId} was applied at step ${facts.faultStep}.`;
  }

  function experimentLine(facts) {
    if (facts === null) {
      return null;
    }
    if (facts.unreadable) {
      return "This run says it is an experiment, but the page cannot read that part of the file.";
    }
    const sentences = [
      "This is an experiment, not something that happened.",
      `It replays run ${facts.sourceRunId}.`,
      faultSentence(facts),
    ];
    if (facts.executedAgainStep !== null) {
      sentences.push(
        `The first step executed again for real was ${facts.executedAgainStep}, attempt ${facts.executedAgainAttempt}.`,
      );
    }
    return sentences.join(" ");
  }

  function isIndexText(value) {
    return isText(value) && value.length <= MAX_INDEX_TEXT_LENGTH;
  }

  function isIndexFile(value) {
    if (!isIndexText(value) || !INDEX_FILE_PATTERN.test(value) || value.includes("..")) {
      return false;
    }
    return !value.split("/").includes(".");
  }

  function isIndexEntry(entry) {
    if (!isObject(entry) || !Object.keys(entry).every((key) => INDEX_KEYS.includes(key))) {
      return false;
    }
    return (
      isIndexFile(entry.file) &&
      isIndexText(entry.run_id) &&
      INDEX_KINDS.includes(entry.kind) &&
      (entry.fault === null || isIndexText(entry.fault)) &&
      (entry.trial === null || Number.isInteger(entry.trial)) &&
      isIndexText(entry.ended) &&
      (entry.outcome === null || isIndexText(entry.outcome))
    );
  }

  function parseRunIndex(text) {
    let index;
    try {
      index = JSON.parse(text);
    } catch {
      throw new Error("The run index is not JSON.");
    }
    const isIndex =
      Array.isArray(index) && index.length <= MAX_INDEX_ENTRIES && index.every(isIndexEntry);
    if (!isIndex) {
      throw new Error("The run index is not valid.");
    }
    return index;
  }

  function compareNullsLast(first, second) {
    if (first === second) {
      return 0;
    }
    if (first === null) {
      return 1;
    }
    if (second === null) {
      return -1;
    }
    return first < second ? -1 : 1;
  }

  function compareChoices(first, second) {
    const byKind = INDEX_RANKS[first.entry.kind] - INDEX_RANKS[second.entry.kind];
    if (byKind !== 0) {
      return byKind;
    }
    const byFault = compareNullsLast(first.entry.fault, second.entry.fault);
    if (byFault !== 0) {
      return byFault;
    }
    const byTrial = compareNullsLast(first.entry.trial, second.entry.trial);
    if (byTrial !== 0) {
      return byTrial;
    }
    return first.position - second.position;
  }

  function choiceLabel(entry) {
    if (entry.kind === "source") {
      return "what was recorded";
    }
    if (entry.kind === "control") {
      return "replayed with nothing changed";
    }
    let label = entry.fault ?? "experiment";
    if (entry.trial !== null) {
      label += `, trial ${entry.trial}`;
    }
    if (entry.outcome !== null) {
      label += `: ${entry.outcome}`;
    }
    return label;
  }

  function pickerChoices(entries) {
    return entries
      .map((entry, position) => ({ entry, position }))
      .sort(compareChoices)
      .map(({ entry }) => ({ file: entry.file, label: choiceLabel(entry) }));
  }

  globalThis.cuadernaLabels = {
    AMOUNT_PATTERN,
    COST_PART_LABELS,
    MAX_VALUE_DEPTH,
    attemptMarks,
    authorizationLabel,
    chainText,
    costPartsAgree,
    costText,
    durationText,
    experimentFacts,
    experimentLine,
    fileSizeProblem,
    isAmount,
    parseRunFile,
    parseRunIndex,
    pickerChoices,
    recordedAuthorization,
    recordedFault,
    recordedReuse,
    statusClass,
    statusLabel,
    sumOfAmounts,
    validSeconds,
  };
})();
