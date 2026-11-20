# Cost and budgets

How a model call gets its cost, how a run is stopped at a budget, and how to compare models.

cuaderna ships no prices. Providers change them by announcement, and an amount stored in a journal should say whose numbers produced it, so the price table is a file you write and keep next to your code:

```json
{
  "format": "cuaderna.prices/1",
  "id": "2026-11-19",
  "currency": "USD",
  "models": [
    {
      "provider": "anthropic",
      "names": ["claude-haiku-4-5", "claude-haiku-4-5-20251001"],
      "rates": {
        "input": "1",
        "output": "5",
        "cache_read": "0.10",
        "cache_write": {"5m": "1.25", "1h": "2"},
        "requests": {"web_search": "10"}
      },
      "variants": [
        {"when": {"service_tier": "batch"}, "rates": {"input": "0.5", "output": "2.5"}}
      ]
    }
  ]
}
```

Token rates are USD per million tokens and request rates USD per thousand requests, written as strings so nothing goes through a float. A model is found by its exact provider and name, with the name in the response tried before the one that was requested. A variant replaces the whole set of rates when everything in its `when` holds: any fact the mapper found in the response (`service_tier`, `speed`, `inference_geo`), or `input_tokens_over` for long-context prices. The first variant that matches wins. `PriceTable.load` reads at most `max_bytes` (1 MiB by default) and raises `PriceTableError` for a file that is larger, unreadable, not UTF-8 or not a price table.

```python
prices = cuaderna.PriceTable.load("prices.json")
engine = cuaderna.Engine("cuaderna.db", prices=prices)

await engine.execute("fix-123", fix_tests, "path/to/repo")

cost = await engine.cost("fix-123")
print(cost.total_usd, cost.unknown_attempts)
for attempt in cost.attempts:
    print(attempt.step_key, attempt.attempt, attempt.usd, attempt.parts)
```

- The cost is computed once, when the attempt is recorded, and stored with it under `cuaderna.cost` together with the `id` of the table and a hash of its content. Editing the table later does not change what past runs cost.
- Failed attempts count. A call that was paid for and then failed to parse is priced as long as `annotate` ran before the exception.
- A model call that cannot be priced has unknown cost, never zero: the table does not have the model, a rate is missing for tokens the call used, or the response carried no usage. `unknown_attempts` counts them, each `AttemptCost` has the `reason`, and `total_usd` adds up only what is known. A model call recorded by an engine with no table is unknown too.
- A cost you already know wins over the table. `cuaderna.annotate(cuaderna.usage.reported_cost("0.0125", "returned by the gateway"))` stores it as reported, which also covers a step that pays for something that is not a model.
- A usage that cannot be read never loses the attempt. If the counts of a response are missing, null, not whole numbers or above 10^12 tokens, the `cuaderna.usage` helpers leave all counts out instead of raising, and the attempt is recorded once with unknown cost and a reason that names the missing field. A cost that cannot be computed is unknown too, with its reason. A rate in a price table is at most 10^9 with at most 18 decimal places, and `PriceTable` refuses a table with a rate beyond that, naming the model and the field.
- One model call per step. `annotate` replaces keys, so a step that makes two calls records the usage of the last one.
- `engine.cost` reads what the journal holds and needs no table, so it works on an imported run.
- Pricing adds 7 microseconds to recording an attempt (median of 7 rounds of 20,000 calls, Python 3.12 on Windows 11), about 1% of the 0.65 milliseconds it takes to record a step.

## A ceiling for a run

```python
@cuaderna.run(budget_usd="2.00")
async def fix_tests(repo: str) -> Summary: ...


try:
    await engine.execute("fix-123", fix_tests, "path/to/repo")
except cuaderna.BudgetExceededError as stop:
    print(stop.spent_usd, stop.budget_usd)
    await engine.execute("fix-123", fix_tests.with_budget("5.00"), "path/to/repo")
```

- The check is before every step attempt, retries included. Once the run has spent its budget or more, the step call raises `BudgetExceededError` and no attempt is recorded.
- Spent is the sum of the cost of every attempt in the journal, failed ones and those of earlier workers included. It is read from the journal when the run is claimed, so a crash does not reset it.
- The run is not failed. It stays unfinished with its journal, and executing it again with a higher budget continues from where it stopped and pays only for what is missing. With the same budget it raises again before running any step.
- Catching the error inside the run function does not get past it: every later step call raises it again, and `execute` raises it when the function ends.
- An unknown cost stops the run too. A ceiling that skips what it cannot price is worse than none, so with a budget set an attempt of unknown cost makes the next step call raise `CostUnknownError`, which says which attempt it was and why. The run stays unfinished in the same way. When it is executed again, attempts of unknown cost are priced with the table the engine has now, from the usage stored with them, so adding the missing model to the table and executing again is the whole fix. The journal keeps what was recorded: `engine.cost` still shows that attempt as unknown. Without a budget nothing stops.
- Nothing is reserved before a call, because the size of an answer is not known beforehand. A run can go past its ceiling by the cost of the steps that were already in flight when it was crossed: one, or as many as it started together with `asyncio.gather`. A call in flight when the process dies leaves no attempt in the journal, so it was paid and is not in the sum.
- `with_budget` returns the same run with another ceiling, for limits that depend on who asks. A budget needs an engine with a price table; without one `execute` raises `InvalidDefinitionError`.

## What another model would have cost

```python
cost = await engine.cost("fix-123")
on_a_larger_model = await engine.reprice("fix-123", provider="anthropic", model="claude-opus-4-5")
print(cost.total_usd, on_a_larger_model.total_usd)
```

`reprice` returns the same shape as `cost`, with every model call of the run priced at the base rates of another model in the engine's table, from the usage stored with each attempt. The journal is not touched. Each of those attempts has the basis `repriced`, and a model the table does not have raises `PriceTableError`.

It is an estimate, and a rough one:

- The same text is not the same number of tokens on another model. Tokenizers differ between providers and between versions of one provider.
- A cache hit on one model is not a hit on another. Cached tokens are priced at the other model's cache rates as if the cache had behaved the same, and a call that used a kind of token or request the other model has no rate for comes back as unknown.
- No variant is applied: batch, priority and long-context prices of the other model are ignored.
- Another model would have answered something else, and the run would have taken other steps.
- A step that is not a model call keeps the cost it reported.
