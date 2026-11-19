# The run page

A static page that shows a recorded run, and the sample run it opens on.

[site](../site) is a static page that shows a run file: the attempts in the order they were recorded, grouped by the process that wrote them, with the arguments, result, error and annotations of each one, the cost of each model call with the parts it is made of, and the total of the run. The page adds up what the file holds and prices nothing itself. It has no build step and no server side. Serve the folder and open it:

```
python -m http.server --directory site
```

It opens on [site/sample-run.json](../site/sample-run.json), a real run of [examples/fix_tests](../examples/fix_tests/README.md), a complete agent that asks a model to fix failing tests, recorded against `claude-haiku-5-5` with the price table in [examples/fix_tests/prices.json](../examples/fix_tests/prices.json). Three processes wrote it:

- The first one had a budget of 0.00005 USD and was killed 0.4 seconds into its model call. It left two attempts. The call that was executing left no record, which is the one call that [can be paid and not counted](cost.md#a-ceiling-for-a-run).
- The second one, with the same budget, took the run over 4.7 seconds later, made the model call again and recorded it at 0.0001764 USD. That is over the budget, so its next step call raised `BudgetExceededError` and the run stayed unfinished. The run went 0.0001264 USD past its ceiling, the cost of the one call in flight when it was crossed.
- The third one was given 0.01 USD and finished the run with two more attempts. The model call was not made again.

`engine.reprice` puts the same tokens on `claude-opus-5-5` at 0.007056 USD, 40 times what the run cost. The budgets are not in the file: a budget belongs to the call to `execute`, not to the journal.

Any other run file can be picked or dropped on the page. It is read in the browser and not uploaded.

Two numbers from that run, measured on the machine that recorded it:

- Its five attempts, with source code and pytest output among their arguments and results, make a file of 6,906 bytes, about 1.4 kB per attempt.
- Live, the run took 6.9 seconds from start to finish, 4.7 of them between the last attempt of the killed process and the first of the next one, most of it waiting for the dead process's lease. Replaying it from the journal takes 0.2 milliseconds (median of 200 replays), because no step executes.
