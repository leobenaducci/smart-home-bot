"""household tests: fork tests whose area has not been ported yet are xfail.

Each entry names the area that makes it pass; the marker is strict, so a
test that starts passing before its entry is removed fails loudly.
"""

import pytest

PENDING: dict[str, str] = {
    "test_skill_invocation_side_doors.py::test_message_tool_": "fork MessageTool.set_context API; the refusal is covered by test_message_tool_refusal.py",
}


NEEDS_MODULE = {
    # Optional extras the fork's image installs (smbprotocol for the share).
    "skills/test_studio_skill.py::test_upload_to_share_needs_a_configured_host": "smbclient",
}


collect_ignore = [
    "api/test_api_attachment.py",  # C2: the fork's document extraction (utils/document.extract_documents)
]


def pytest_collection_modifyitems(config, items):
    import importlib.util

    for item in items:
        nodeid = item.nodeid.split("tests/household/", 1)[-1]
        for prefix, module in NEEDS_MODULE.items():
            if nodeid.startswith(prefix) and importlib.util.find_spec(module) is None:
                item.add_marker(pytest.mark.skip(reason=f"optional module {module} not installed"))
        for prefix, reason in PENDING.items():
            if nodeid.startswith(prefix):
                item.add_marker(pytest.mark.xfail(reason=reason, strict=True))
