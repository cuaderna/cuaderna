(() => {
  const STATUS_CLASSES = {
    ok: "ok",
    error: "error",
    running: "running",
    completed: "completed",
    failed: "failed",
    imported_unfinished: "imported_unfinished",
  };
  const RUN_FILE_FORMAT = "cuaderna.run/1";
  const MAX_FILE_BYTES = 16 * 1024 * 1024;
  const MAX_VALUE_DEPTH = 64;
  const AMOUNT_PATTERN = /^\d{1,30}(\.\d{1,30})?$/;
  const TIME_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
  const AUTHORIZATION_REASONS = ["rule", "no_rule", "unevaluable", "rejected"];
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
      file.format === RUN_FILE_FORMAT &&
      hasTextFields(file.run, ["run_id", "name", "status", "created_at"]) &&
      Array.isArray(file.attempts) &&
      file.attempts.every(
        (attempt) =>
          hasTextFields(attempt, ["step_key", "name", "status", "started_at", "finished_at"]) &&
          hasObjectOrNoAnnotations(attempt),
      );
    if (!isRunFile) {
      throw new Error(`This is not a run file in the ${RUN_FILE_FORMAT} format.`);
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

  globalThis.cuadernaLabels = {
    AMOUNT_PATTERN,
    COST_PART_LABELS,
    MAX_VALUE_DEPTH,
    authorizationLabel,
    chainText,
    costPartsAgree,
    costText,
    durationText,
    fileSizeProblem,
    isAmount,
    parseRunFile,
    recordedAuthorization,
    statusClass,
    statusLabel,
    sumOfAmounts,
    validSeconds,
  };
})();
