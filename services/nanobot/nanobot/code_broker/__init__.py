"""The only thing in this house that touches a git remote.

See ALFRED_PROGRAMADOR.md. The short version: the agent's shell runs
unsandboxed beside 29 credentials, so per-project keys cannot live there. They
live here instead, in a container with none of those, and the agent gets verbs
rather than secrets.
"""

from nanobot.code_broker.workspace import BrokerError, Credential, Workspace

__all__ = ["BrokerError", "Credential", "Workspace"]
