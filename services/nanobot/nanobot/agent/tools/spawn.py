"""Spawn tool for creating background subagents."""

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager


@tool_parameters(
    tool_parameters_schema(
        task=StringSchema("The task for the subagent to complete"),
        context=StringSchema(
            "Relevant background from this conversation the subagent needs to do the task. "
            "The subagent CANNOT see this conversation, so resolve every reference here: "
            "what the user asked, what a 'yes'/'that one'/'the second option' refers to, "
            "prior decisions, filenames, and any details it would otherwise be missing."
        ),
        label=StringSchema("Optional short label for the task (for display)"),
        complex=BooleanSchema(
            "true: run on the more capable model (difficult or open-ended work). "
            "false: the fast model. Leave it out and the house decides from the task itself.",
            nullable=True,
        ),
        required=["task"],
    )
)
class SpawnTool(Tool):
    """Tool to spawn a subagent for background task execution."""

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel: ContextVar[str] = ContextVar("spawn_origin_channel", default="cli")
        self._origin_chat_id: ContextVar[str] = ContextVar("spawn_origin_chat_id", default="direct")
        self._session_key: ContextVar[str] = ContextVar("spawn_session_key", default="cli:direct")

    def set_context(self, channel: str, chat_id: str, effective_key: str | None = None) -> None:
        """Set the origin context for subagent announcements."""
        self._origin_channel.set(channel)
        self._origin_chat_id.set(chat_id)
        self._session_key.set(effective_key or f"{channel}:{chat_id}")

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "The subagent will complete the task and report back when done. "
            "IMPORTANT: the subagent starts with a fresh context and CANNOT see this "
            "conversation. Pass any background it needs via the 'context' argument and "
            "write a self-contained 'task' — never rely on it knowing what was just said. "
            "For deliverables or existing projects, inspect the workspace first "
            "and use a dedicated subdirectory when helpful. Set complex=true for tasks that are especially difficult or open-ended so a more capable model is used, complex=false to insist on the fast one; leave it out to let the house classify the task."
        )

    async def execute(
        self,
        task: str,
        context: str | None = None,
        label: str | None = None,
        complex: bool | None = None,
        **kwargs: Any,
    ) -> str:
        """Spawn a subagent to execute the given task.

        ``complex`` left unset means the spawner did not say; the manager then
        asks the turn classifier -- see agent/classify.py.
        """
        return await self._manager.spawn(
            task=task,
            context=context,
            label=label,
            complex=complex,
            origin_channel=self._origin_channel.get(),
            origin_chat_id=self._origin_chat_id.get(),
            session_key=self._session_key.get(),
        )
