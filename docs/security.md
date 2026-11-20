# Security

What cuaderna protects, and what it does not. To report a problem, see [SECURITY.md](../SECURITY.md).

## What it protects

A [policy](permissions.md) is a check on step calls inside a process that cooperates with it. Before a step runs, the call is decided against the rules, a person can be asked first, and the journal records what was decided. A [budget](cost.md) stops a run before its next step once the recorded cost reaches it. The journal can be checked against a value kept elsewhere, as described in [checking the journal with verify](permissions.md#checking-the-journal-with-verify).

## What it does not protect

- **It is not a sandbox.** The code of a step and the code between steps can do anything the process can, and is not checked. A step that runs a program runs whatever the model wrote into that program's inputs.
- **Paths are compared as text.** A symbolic link or a junction inside an allowed directory leads outside it, and `under` does not see that. A step that follows links must check where it lands.
- **Names are claims.** `actor` and the approver's name are recorded as given by whoever holds the engine. They are not verified identities, and anything that can call `approve` can approve.
- **The journal is plain text.** Anyone who can write the file can recompute its hashes. Tamper evidence needs the value from `verify` kept outside the file. A journal and a run file also hold prompts, arguments and results as they were, and nothing is purged.
- **A budget counts annotated calls only.** A model call that a step does not annotate with its usage is not counted, and the call in flight can go over. `max_steps` on the engine is the limit that does not depend on annotations.
- **Files show what they say.** A run file you did not produce can claim anything. An [imported run](usage.md) is marked as imported and cannot be executed, and its audit entries are what the file says. The [run page](run-page.md) shows what the file says and does not check it.
