from pathlib import Path

import pytest
from examples.fix_tests import compare

_INDEX = Path(__file__).parent.parent / "site" / "runs.json"


async def test_the_agent_that_gives_up_ends_where_the_first_one_failed() -> None:
    changes = {
        change.fault: change
        for change in await compare.compare(
            _INDEX, _INDEX, before_agent="agent", after_agent="gives-up"
        )
    }

    assert dict(changes["model-down"].before.outcomes) == {"failed with ConnectionError": 3}
    assert dict(changes["model-down"].after.outcomes) == {"gave up": 3}
    assert dict(changes["tests-unavailable"].before.outcomes) == {"failed with OSError": 3}
    assert dict(changes["tests-unavailable"].after.outcomes) == {"gave up": 3}
    assert dict(changes[None].after.outcomes) == {"fixed": 1}
    assert changes["wrong-edit"].after.trials == 0


async def test_an_index_with_several_agents_needs_one_picked() -> None:
    with pytest.raises(SystemExit, match="several agents"):
        await compare.compare(_INDEX, _INDEX, before_agent=None, after_agent="gives-up")
