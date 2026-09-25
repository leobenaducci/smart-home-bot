"""What the Models page's Test button must catch.

Run: ./.venv/bin/python admin/test_probe.py

The button exists because the page could say what a model costs and never
whether it works. Everything here is a fault this household has actually been
sent, and the three harmony shapes are the reason a single check is not
enough -- they differ only by how much of the markup the gateway ate on the
way out, and a detector that knows one passes a model that is broken.

The same samples are in `services/nanobot/tests/utils/test_strip_harmony.py`,
which tests the *stripping*. This tests the *detecting*. They are deliberately
separate code -- the admin page must not import the assistant -- so the shared
samples are what keeps them honest about the same faults.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import models as model_catalogue  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append((label, detail))


print("harmony, in the three shapes the household was sent")

# 1. Nothing eaten -- the markers arrive intact.
check("markers intact are caught",
      model_catalogue.harmony_leak("<|channel|>final<|message|>Juana salió de casa."))
# 2. Markers eaten, channel name welded to the words. Sent 2026-09-02 as
#    "finalJuana salió de casa."
check("a bare channel name opening the reply is caught",
      model_catalogue.harmony_leak("finalJuana salió de casa."))
# 3. All of it eaten. Sent 2026-09-02 10:37, a whole reasoning trace with the
#    real answer as its last seven characters.
check("a fully unmarked reasoning trace is caught",
      model_catalogue.harmony_leak(
          'analysisWe need to interpret the request. I will respond '
          '"Hecho."assistantfinalHecho.'))

# ...and the same with no final channel at all, which is what a model that
# runs out of budget mid-thought returns. Measured on gpt-oss-20b: 400 tokens
# of unbroken analysis and no answer. Only the opening rule sees this one --
# there is no `final` anywhere for the boundary rule to find.
check("reasoning that never reached an answer is caught",
      model_catalogue.harmony_leak(
          "analysisWe need to provide an answer. The user asked about the "
          "weather and we should check it first."))

print("shapes that do not open on a channel name")

check("a reply that is only a final channel is caught",
      model_catalogue.harmony_leak("assistantfinalHecho."))
check("a leak after real content is caught",
      model_catalogue.harmony_leak("Mora llegó.assistantfinalMora llegó."))
check("a think block is caught",
      model_catalogue.harmony_leak("<think>hidden</think>visible"))

print("prose that merely resembles it is left alone")

for text in ("Juana salió de casa.",
             "Analysis of the bill is attached",
             "The final Answer is 42",
             # The one the no-space guard is for: a person asking about code.
             "I have set the finalAnswer variable",
             "I've said 'ready' to the household."):
    check(f"clean: {text[:40]!r}", not model_catalogue.harmony_leak(text))

print("the probe reports it as a failure, not a pass")

# A probe that finds markup must fail the model. `probe_model` is not called
# here -- that needs a network -- but the step it would fail on reads from the
# same helper, so an empty verdict on dirty text is the regression to catch.
check("a dirty reply never reads as clean",
      all(model_catalogue.harmony_leak(t) for t in (
          "<|channel|>final<|message|>x",
          "finalJuana salió.",
          "analysisReasoning.assistantfinalHecho.",
          "assistantfinalHecho.")))

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for label, detail in failures:
        print(f"  - {label}: {detail}")
    sys.exit(1)
print("all checks passed")
