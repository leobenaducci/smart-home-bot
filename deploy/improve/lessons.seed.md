# Lessons

What went wrong before, so it does not again. Read it before proposing
anything; add to it (`improve lesson "…"`) when the person tells you that you
got something wrong.

- **A claim the assistant makes about itself is a lead, not a fact.** The
  everyday model cannot see its own code, prompt or logs. "I read the whole
  SKILL_PYTHON.md for every call" was built into a plan, and nine days of
  sessions showed it was not so. Measure (`improve measure`) or read the code
  before building on it.
- **Skill translation runs locally and needs the guide.** `SKILL_PYTHON.md`
  defines the functions a call names; skipping it makes every call a NameError,
  and it costs no model tokens anyway.
- **Professions are profiles of one instance.** Disabling a skill "for users"
  disables it for Designer too.
- **`self-improve` is how fix requests reach the Programmer.** Do not redirect
  it to `profession`.
- **Never change a recorded measurement, or the evidence in a comment, to fit a
  plan.** If the evidence disagrees with the plan, the plan changes.
- **Demoting an always-loaded skill** needs its description to carry its actions
  and how to invoke them, and the benchmark before and after. Changing the flag
  and the comment around it is not the change.
- **What is specific to this house goes in its live config or a plugin**, never
  in the stack's shipped files.
- **A question is not a yes.** Implement only after the person agrees to the
  plan in so many words; "check this" means check, and report.
- **One change, measured, then the next.** Six skills at once cannot tell you
  which of them made the benchmark move.
