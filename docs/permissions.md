# Permissions

What a run may do, how a person approves a call, and how to read what was decided.

A policy is a check in the same process, between the model's choice of step and arguments and the effect. It is not a sandbox: the code of a step, and the code between steps, can do anything the process can and is not checked.

```json
{
  "format": "cuaderna.policy/1",
  "id": "fix-tests-1",
  "rules": [
    {"id": "look", "effect": "allow", "steps": ["run_tests", "ask_model"]},
    {"id": "edit-src", "effect": "allow", "steps": ["write_file"], "when": {"edit.path": {"under": "src"}}},
    {"id": "edit-tests", "effect": "ask", "steps": ["write_file"], "when": {"edit.path": {"under": "tests"}}},
    {"id": "never-ci", "effect": "deny", "steps": ["write_file"], "when": {"edit.path": {"under": ".github"}}}
  ]
}
```

```python
policy = cuaderna.Policy.load("policy.json")
engine = cuaderna.Engine("cuaderna.db", actor="ci-runner")


@cuaderna.run(policy=policy)
async def fix_tests(repo: str) -> Summary: ...


try:
    await engine.execute("fix-123", fix_tests, "repo")
except cuaderna.ApprovalRequiredError:
    for request in await engine.pending("fix-123"):
        await engine.approve("fix-123", request.step_key, by="maxi", note="checked the diff")
    await engine.execute("fix-123", fix_tests, "repo")
```

- A policy belongs to the run definition, and `with_policy(other)` swaps it. A run without one checks nothing. `cuaderna.now`, `random` and `new_id` are never checked.
- Every rule that names the step and whose `when` holds is a match. Any deny wins, then any ask, then any allow; no match denies. Order in the file means nothing. A rule whose `when` names an argument the call lacks, or whose value has the wrong type, cannot be evaluated and denies with the reason `unevaluable`.
- A path in `when` is a parameter name with dots into objects, read from the arguments as stored. All paths must hold. Conditions: `equals`, `in` (a list), `glob` (`fnmatch`, where `*` also matches `/`) and `under` (a relative path that stays inside a directory).
- Use `under` for paths, not `glob`: `src/../.github/x` matches `src/*`. `under` rejects absolute paths and any `..` that leaves the directory, but it does not resolve symbolic links, so a step that follows links must check where it lands.
- The check runs before every attempt, retries included. A denied call records an attempt with status error whose step never ran, and raises `PermissionDeniedError` with `reason` and `rules`. Caught in the run, the run goes on; on resume and replay the same call raises the same error from the journal, even if the policy changed.
- An `ask` records a request and raises `ApprovalRequiredError`. The run stays unfinished, like a run stopped at its budget, and a run stops at the first ask. `engine.pending(run_id=None)` lists open requests, for one run or for the whole journal. `engine.approve(run_id, step_key, *, by, note=None)` and `engine.reject`, with the same arguments, answer it once, from any process, and executing the run again continues from the journal. A rejection is a denial with the reason `rejected`.
- An approval is bound to the exact arguments. If the run reaches the step key with other arguments, a new request is made. The current policy decides first: an approval only matters while the decision is `ask`.
- `actor` and `by` are recorded claims, not verified identities.

## Reading what was decided

```python
for entry in await engine.audit("fix-123"):
    print(entry.at, entry.actor, entry.step_key, entry.decision, entry.rules, entry.outcome)
```

`audit` returns one `AuditEntry` per attempt, in the order recorded: `at`, `step_key`, `name`, `attempt`, `args`, `lease_token`, `outcome` (`ok`, `error` or `denied`), `decision`, `effect`, `reason`, `policy`, `policy_hash`, `rules`, `actor`, `approved_by`, `approved_at` and `approval_note`. An attempt recorded without a policy has `decision` `None`. It reads the journal and needs no policy, so it works on an imported run.

`engine.evaluate(run_id, policy)` returns, as `StepDecision` objects with `step_key`, `name`, `args` and `decision`, what another policy would decide for each step call of a recorded run, without executing anything. It is not a prediction: a denial would have changed what the model did next.

## Checking the journal was not edited

`await engine.verify(run_id)` returns the head of the run's hash chain, or raises `JournalTamperedError` with `run_id`, `step_key` and `attempt` of the first attempt that does not match. Every attempt stores a hash that covers the attempt recorded before it and its own fields, so an edited, removed or reordered attempt breaks the chain from there on. Triggers in the journal also refuse any update or delete of an attempt, an approval request or its answer.

- Keep the returned head somewhere outside the journal file, such as a commit or a ticket. Someone who can write the file can recompute the whole chain and drop the triggers, and removing the last attempts only moves the head.
- The chain covers the attempts, not the final status and output of the run.
- Attempts recorded before this version have no hash, and their runs do not verify.
- A run file exported with `redact=` does not verify, because the hashes cover the values as they were recorded. Run files written without it verify in another journal, also under `as_run_id`.

Measured on the machine that recorded the [sample run](run-page.md): deciding on one of its step calls takes between 2 and 4 microseconds (median of 2,000 decisions per call), and verifying a run of 1,000 attempts takes about 8 milliseconds (median of 5).
