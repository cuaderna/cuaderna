from examples.fix_tests import agent

import cuaderna


class Summary(agent.Summary):
    gave_up: str | None = None


@cuaderna.run(policy=agent.fix_tests.policy)
async def fix_tests(repo: str, provider: str, model: str, max_rounds: int) -> Summary:
    rounds = 0
    try:
        report = await agent.run_tests(repo)
        refusal = ""
        while not report.passed and rounds < max_rounds:
            sources = await agent.read_sources(repo)
            edit = await agent.ask_model(provider, model, sources, report.output + refusal)
            try:
                await agent.write_file(repo, edit)
                refusal = ""
            except cuaderna.PermissionDeniedError as error:
                refusal = f"\n\n# your last edit, to {edit.path}, was refused\n{error}"
            report = await agent.run_tests(repo)
            rounds += 1
    except cuaderna.StepFailedError as error:
        return Summary(fixed=False, rounds=rounds, gave_up=_what_failed(error))
    return Summary(fixed=report.passed, rounds=rounds)


def _what_failed(error: cuaderna.StepFailedError) -> str:
    step_name = error.step_key.partition("#")[0]
    return f"{step_name} failed with {error.error_type}"
