"""The benchmark's cases are its answer key, and the assistant it measures
runs in the same container with a shell. A per-run copy is deleted as soon as
it is read; anything else is left where it is."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bench"))
import model_bench as M  # noqa: E402


def test_a_per_run_copy_is_gone_once_read():
    run = Path(tempfile.mkdtemp(prefix="bench-run-", suffix="-bench", dir="/tmp"))
    cases = run / "cases.json"
    cases.write_text("{}", encoding="utf-8")
    M.forget_cases(str(cases))
    assert not cases.exists()


def test_a_checkout_copy_stays():
    here = Path(tempfile.mkdtemp()) / "bench"
    here.mkdir()
    cases = here / "cases.json"
    cases.write_text("{}", encoding="utf-8")
    M.forget_cases(str(cases))
    assert cases.exists()
