"""Nothing credential-shaped leaves the broker client.

Everything these tools return is copied verbatim into a conversation, and a
conversation is stored, summarised and shown to a person. A credential that
lands in one has not been logged; it has been published.
"""

from alfred_mcp.broker import retarget, scrub


def test_a_credential_is_replaced_not_passed_through():
    out = scrub({"auth_kind": "ssh_key", "auth_secret": "-----BEGIN KEY-----"})
    assert out["auth_secret"] == "(removed)"
    # The useful half survives: which kind of credential it is says something
    # the agent may legitimately need, and says nothing about its value.
    assert out["auth_kind"] == "ssh_key"


def test_the_key_stays_so_the_removal_is_visible():
    """Dropped, it would read as a field the broker did not send."""
    assert "auth_secret" in scrub({"auth_secret": "x"})


def test_it_reaches_into_nested_structures():
    out = scrub({"project": {"name": "web", "credentials": [
        {"id": 3, "private_key": "x"}, {"id": 4, "api_key": "y"}]}})
    creds = out["project"]["credentials"]
    assert creds[0]["private_key"] == "(removed)"
    assert creds[1]["api_key"] == "(removed)"
    assert creds[0]["id"] == 3 and out["project"]["name"] == "web"


def test_it_matches_on_the_name_not_the_value():
    """A value that looks like a hash is not necessarily a secret, and a secret
    does not necessarily look like one."""
    out = scrub({"commit": "a3f19c8e" * 5, "session_token": "hello"})
    assert out["commit"] == "a3f19c8e" * 5
    assert out["session_token"] == "(removed)"


def test_what_the_project_list_actually_returns_is_kept():
    """The real shape, from a live `list_projects`. These fields are what the
    agent picks a project with, and none of them is the credential itself."""
    row = {"name": "Fracciones", "auth_kind": "ssh_key", "has_auth": True,
           "credential_id": 3, "credential_name": "Azure DevOps SSH Key",
           "git_url": "user@host:v3/org/repo/repo",
           "protected_paths": ["users.json", ".env", "*.pem"]}
    assert scrub({"projects": [row]})["projects"][0] == row


def test_an_empty_answer_survives():
    assert scrub({}) == {}
    assert scrub({"projects": []}) == {"projects": []}


# --- the two names one directory has --------------------------------------

def test_a_checkout_path_is_rewritten_for_the_agent():
    """The broker names it from a container; opencode opens it from the host.

    Handing the broker's own path straight through told the agent to read and
    edit a directory that does not exist on the machine it runs on. Every tool
    call failed or asked to leave the session directory, and the turn stopped
    without saying why -- which is what "it is not doing anything" looked like.
    """
    out = retarget({"path": "/nanobot-code-workspace/user1/web"},
                   "/nanobot-code-workspace", "/mnt/data/state/workspace")
    assert out["path"] == "/mnt/data/state/workspace/user1/web"


def test_the_root_itself_is_rewritten_too():
    out = retarget({"path": "/nanobot-code-workspace"},
                   "/nanobot-code-workspace", "/host/ws")
    assert out["path"] == "/host/ws"


def test_a_sibling_directory_is_left_alone():
    """`/workspace` must not match `/workspace-backup`.

    Without the separator the prefix check rewrites a path into a directory
    that is not the one it names -- silently, and only for the neighbours.
    """
    out = retarget({"path": "/nanobot-code-workspace-old/user1"},
                   "/nanobot-code-workspace", "/host/ws")
    assert out["path"] == "/nanobot-code-workspace-old/user1"


def test_only_values_under_a_path_key_are_touched():
    """A commit message or a branch may quote the root and is not a path."""
    out = retarget({"message": "moved /nanobot-code-workspace/user1 into git",
                    "branch": "/nanobot-code-workspace/x"},
                   "/nanobot-code-workspace", "/host/ws")
    assert out["message"] == "moved /nanobot-code-workspace/user1 into git"
    assert out["branch"] == "/nanobot-code-workspace/x"


def test_it_reaches_nested_structures_like_scrub_does():
    out = retarget({"projects": [{"path": "/ws/user1/a"}, {"path": "/ws/user1/b"}]},
                   "/ws", "/host/ws")
    assert [p["path"] for p in out["projects"]] == ["/host/ws/user1/a",
                                                    "/host/ws/user1/b"]


def test_no_translation_when_the_two_roots_are_the_same():
    """A deployment where the paths already agree must not be rewritten."""
    same = {"path": "/ws/user1"}
    assert retarget(same, "/ws", "/ws") == same
    assert retarget(same, "/ws", "") == same
    assert retarget(same, "", "/host") == same
