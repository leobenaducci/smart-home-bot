"""A skill block that resolves to no call ends the turn as `bad_invocation`.

It used to end as `completed`, with a note glued into the content: the person
saw a sentence promising an action and nothing happened, and nothing upstream
could tell that turn from a good one. Naming it is what lets the loop hand the
same turn to a stronger model (see agent/classify.py).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.runner import AgentRunSpec, AgentRunner, _DEFAULT_ERROR_MESSAGE  # noqa: F401
from nanobot.providers.base import LLMResponse


async def _run(tmp_path, content: str):
    skill_dir = tmp_path / "real-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Real Skill\nDoes stuff.\n")
    system_msg = f"Skills:\n- **real-skill** `{skill_dir / 'SKILL.md'}`\n"

    provider = MagicMock()

    async def chat_with_retry(*, messages, **kwargs):
        return LLMResponse(content=content, tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.tool_names = ["read_file"]
    tools.execute = AsyncMock(return_value="content")
    return await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "system", "content": system_msg},
                          {"role": "user", "content": "hacé algo"}],
        tools=tools, model="test-model", max_iterations=3, max_tool_result_chars=16_000,
    ))


@pytest.mark.asyncio
async def test_an_invocation_of_a_skill_that_does_not_exist_is_bad_invocation(tmp_path):
    result = await _run(tmp_path, 'Dale.\n{"skill": "no-such-skill", "action": "go"}')
    assert result.stop_reason == "bad_invocation"
    assert "No pude usar no-such-skill" in (result.final_content or "")
    assert "no-such-skill" not in (result.final_content or "").split("[")[0]  # the block itself is gone


@pytest.mark.asyncio
async def test_a_plain_answer_is_still_completed(tmp_path):
    result = await _run(tmp_path, "Listo, ya está.")
    assert result.stop_reason == "completed"
    assert result.final_content == "Listo, ya está."
