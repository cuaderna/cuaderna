# The run page

A static page that shows a recorded run, and the sample run it opens on.

[site](../site) is a static page that shows a run file: the attempts in the order they were recorded, grouped by the process that wrote them, with the arguments, result, error and annotations of each one, the cost of each model call with the parts it is made of, what each step was allowed to do and who approved it, and the total of the run. The page adds up what the file holds and prices nothing itself. It has no build step and no server side. Serve the folder and open it:

```
python -m http.server --directory site
```

It opens on [site/sample-run.json](../site/sample-run.json), a real run of [examples/fix_tests](../examples/fix_tests/README.md), a complete agent that asks a model to fix failing tests, recorded against `claude-haiku-5-5` with the price table in [examples/fix_tests/prices.json](../examples/fix_tests/prices.json). Three processes wrote it:

- The first one had a budget of 0.00005 USD and was killed 0.4 seconds into its model call. It left two attempts. The call that was executing left no record, which is the one call that [can be paid and not counted](cost.md#a-ceiling-for-a-run).
- The second one, with the same budget, took the run over 4.7 seconds later, made the model call again and recorded it at 0.0001774 USD. That is over the budget, so its next step call raised `BudgetExceededError` and the run stayed unfinished. The run went 0.0001274 USD past its ceiling, the cost of the one call in flight when it was crossed.
- The third one was given 0.01 USD and finished the run with two more attempts. The model call was not made again.

The run was recorded under an earlier version of the policy in [examples/fix_tests/policy.json](../examples/fix_tests/policy.json), whose rule `edit-package` allowed an edit of any file under the repo root; the policy now allows an edit of `pricing.py` only. Its five step calls were all allowed, four by the rule `look` and the edit by `edit-package`; none needed a person and none was denied. The file was exported with local paths hidden, so its hash chain [does not verify](permissions.md#checking-the-journal-with-verify): the hashes cover the values as they were recorded.

`engine.reprice` puts the same tokens on `claude-opus-5-5` at 0.007096 USD, 40 times what the run cost. The budgets are not in the file: a budget belongs to the call to `execute`, not to the journal.

Any other run file can be picked or dropped on the page. It is read in the browser and not uploaded. A file larger than 16 MiB is refused before it is read, a value nested more than 64 levels deep is shown as "nested too deeply to show", and an amount of more than 30 digits before or after the point is shown as an unknown cost. A file that cannot be read, is not UTF-8 text, is not JSON or cannot be shown leaves the run that was open as it was and says why in the status line.

When [site/runs.json](../site/runs.json) lists more than one run, the page shows a picker above the file input: what was recorded first, then the run replayed with nothing changed, then the [fault experiments](faults.md) grouped by fault, each with its trial and outcome if the index says them. Picking one loads that file the same way as a dropped one. The index is read like a run file, as untrusted input: at most 200 entries, each with exactly the keys the page knows, and a `file` that is a relative path of letters, digits, dots, dashes and underscores ending in `.json`, with no `..` and no scheme. One bad entry makes the whole index invalid, and a missing or invalid index leaves the page as it is without it. Opening a file by hand does not need an index.

A run file in the `cuaderna.run/2` format that marks the run as an experiment gets one line under the status: that it is an experiment and not something that happened, the run it replays, the fault and the step where it was applied (or that nothing was changed), and the first step that had to be executed again for real, if there is one. An attempt the fault was injected into is labelled "injected" and one answered from the recording "from the recording", next to "denied" and "approved"; its details say which fault and kind, whether the step itself ran, and which run and attempt the answer came from.

The page carries a Content-Security-Policy and a no-referrer policy in meta tags, but `frame-ancestors` cannot be set from a meta tag, so whoever hosts the page should send the header `Content-Security-Policy: frame-ancestors 'none'`.

The page shows what the file says and nothing more. A status it does not know is shown as an unknown status, a cost that is missing or cannot be read as "unknown", a time that is negative or out of range as "invalid time", and an authorization that is missing or unreadable as "not recorded" or "unreadable", never as "ok". Each attempt says whether it carries a chain hash, but the page does not check the chain: use `verify` for that. The labels are plain functions in [site/labels.js](../site/labels.js), tested with `node --test site/labels.test.js`.

Two numbers from that run, measured on the machine that recorded it:

- Its five attempts, with source code and pytest output among their arguments and results, make a file of 9,019 bytes, about 1.8 kB per attempt.
- Live, the run took 6.9 seconds from start to finish, 4.7 of them between the last attempt of the killed process and the first of the next one, most of it waiting for the dead process's lease. Replaying it from the journal takes 0.2 milliseconds (median of 200 replays), because no step executes.
