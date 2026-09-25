"""Shared test doubles.

`mock_provider` exists because a bare `MagicMock()` stopped being a usable
stand-in for a provider. The loop now asks it which model is serving the turn
(`LLMProvider.serving_model`, so the agent can tell the family the truth when
an outage has moved them onto the fallback) and unpacks a pair from the answer
— and a MagicMock's iteration protocol yields nothing, so every test that
drives a turn through a hand-rolled double failed on the unpack rather than on
anything it was written to check.

One factory, so the next question the loop learns to ask a provider is answered
here once instead of in the nine places that had built the double by hand.
"""
from unittest.mock import MagicMock

import pytest

from nanobot.providers.base import LLMProvider
from nanobot.utils.outage_store import SHARED_OUTAGES


@pytest.fixture(autouse=True)
def _no_outage_windows_between_tests(tmp_path):
    """Outage windows are keyed per gateway and shared by every provider
    instance reaching it — deliberately, so one route's discovery covers the
    others. That also means a test that marks a model down at some api_base
    would hand the next test a provider that quietly routes around it, in a
    file that never mentions outages. Cleared on the way in and out.

    Both halves, not only the in-process one. `_mark_model_down` also publishes
    to `SHARED_OUTAGES`, whose default directory is the live `/shared-state`
    mount — and the deployer runs this suite inside the built image before it
    replaces a running container. Left pointed there, a test that marks
    `gpt-5.6-luna` down at the real gateway tells all six assistants in the
    house that the model they run on is dead for the next hour, and
    `test_the_shared_window_expires_for_everyone_at_once` fails because a
    sibling test's file outlives its own in-process window. Per-test tmp_path,
    restored afterwards so nothing inherits a stale singleton.
    """
    was = SHARED_OUTAGES.path.parent
    SHARED_OUTAGES.use_dir(tmp_path / "shared-state")
    LLMProvider._OUTAGE_WINDOWS.clear()
    yield
    LLMProvider._OUTAGE_WINDOWS.clear()
    SHARED_OUTAGES.use_dir(was)


def mock_provider(default_model: str = "test-model") -> MagicMock:
    """A MagicMock that answers the questions AgentLoop actually asks."""
    provider = MagicMock()
    provider.get_default_model.return_value = default_model
    # No outage window in a test double: the model asked for is the model
    # serving, and nothing is displaced. A test about the fallback wants a real
    # provider — see tests/agent/test_serving_model_is_in_the_prompt.py.
    provider.serving_model.side_effect = lambda model=None: (model or default_model, None)
    return provider
