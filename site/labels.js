(() => {
  const STATUS_CLASSES = {
    ok: "ok",
    error: "error",
    running: "running",
    completed: "completed",
    failed: "failed",
    imported_unfinished: "imported_unfinished",
  };
  const AUTHORIZATION_REASONS = ["rule", "no_rule", "unevaluable", "rejected"];

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
    authorizationLabel,
    chainText,
    recordedAuthorization,
    statusClass,
    statusLabel,
  };
})();
