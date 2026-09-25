"""Three ways the bench marked good work wrong, and the rules that fixed them.

Measured across 23 models on 2026-09-22: rent_report_pdf failed on every one,
including deepseek-v4-flash, because the strong models hand long work to a
sub-agent the bench stubs; research_topic failed a sourced table for having no
bullet points; svg_drawing_html failed a working page written with write_file.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

_spec = importlib.util.spec_from_file_location(
    "model_bench", Path(__file__).resolve().parents[2] / "bench" / "model_bench.py")
MB = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(MB)


def rec(*calls):
    return SimpleNamespace(calls=[{"label": label, "detail": detail, "ran": False}
                                  for label, detail in calls])


RENT = {"id": "rent_report_pdf", "delegate_ok": True,
        "expect_tools": ["web_search|web_fetch", "skill(-read)?:document|create_doc|^exec$"],
        "reply": {"nonempty": True, "min_chars": 400}}
TASK = '{"task": "Buscar departamentos de 2 dormitorios en arriendo en Ñuñoa y armar un informe PDF"}'


def test_a_real_spawn_takes_the_route_and_skips_length():
    assert MB.judge(RENT, "Me pongo con eso.", rec(("spawn", TASK)), None) == []


def test_a_one_line_spawn_does_not():
    fails = MB.judge(RENT, "Me pongo con eso.", rec(("spawn", '{"task": "x"}')), None)
    assert any("expected a call" in f for f in fails)


def test_spawn_counts_only_where_the_case_allows_it():
    case = {k: v for k, v in RENT.items() if k != "delegate_ok"}
    assert MB.judge(case, "ok", rec(("spawn", TASK)), None)


TABLE = """Resumen:

| Punto | Descripción | Fuente |
| :--- | :--- | :--- |
| Fijación | Cada cuatro años | CGE |
| Principio | Costos reales | CNE |
| Opción | Baja tensión | Creara |
"""


def test_table_rows_count_as_items_but_not_the_header():
    case = {"id": "t", "reply": {"lines_min": 3}}
    assert MB.judge(case, TABLE, rec(), None) == []
    assert MB.judge({"id": "t", "reply": {"lines_min": 4}}, TABLE, rec(), None)


def test_a_list_still_counts():
    case = {"id": "t", "reply": {"lines_min": 3}}
    assert MB.judge(case, "- a\n- b\n- c\n", rec(), None) == []


# --- long tasks on the harness ------------------------------------------------

def test_harness_calls_read_as_the_labels_the_cases_use():
    from nanobot.harness.pi_runner import ToolCall
    lab = MB.harness_label
    assert lab(ToolCall("web", {"action": "search"})) == ("web_search", True)
    assert lab(ToolCall("web", {"action": "fetch"})) == ("web_fetch", True)
    assert lab(ToolCall("make_document", {})) == ("skill:document.create_doc", False)
    assert lab(ToolCall("skill", {"skill": "paperless", "action": "search_documents"})) == \
        ("skill:paperless.search_documents", True)
    assert lab(ToolCall("skill", {"skill": "grocery", "action": "add_grocery"})) == \
        ("skill:grocery.add_grocery", False)
    assert lab(ToolCall("skill_guide", {"skill": "document"})) == ("skill-read:document", True)


def test_a_skill_script_run_by_hand_is_a_write():
    """The leak: `python3 …/create_doc.py '{…}'` filed real guides from the bench."""
    cmd = "python3 /app/nanobot/skills/document/create_doc.py '{\"format\": \"pdf\"}'"
    assert MB.EXEC_WRITE_RE.search(cmd)
    assert not MB.EXEC_WRITE_RE.search("cat /app/nanobot/skills/document/SKILL.md")
