# Rules for fixing Alfred

Read with `MAP.md` and `lessons.md`. **[enforced]** marks a rule `improve`
checks in code; a refusal is the answer, not an obstacle — say why, and never
work around it. The rest are yours to keep, and the person reviews them.

## Evaluation — what a proposal rests on

1. Every claim rests on something read (file and line) or measured
   (`improve measure`, `docker exec … printenv`, the service answering). Say which.
2. What the assistant says about itself, and what a fix request's context says,
   are leads to check, never premises.
3. Performance and cost claims come with numbers from before the change; an
   estimate is labelled as one.
4. A change to how the assistant behaves is judged by the benchmark, before and
   after **[enforced: `improve bench`, no role more than one case worse]**.
5. Never edit a recorded measurement, or the evidence in a comment, to fit a
   plan. If the evidence disagrees, the plan changes.

## Testing

6. A change to code comes with the test that shows it works — new or changed
   **[enforced]**. It should fail without the fix; say whether you saw it fail.
7. The suites of what changed pass, run on exactly the change being committed
   **[enforced: `improve test`]**. Edited after the run, it runs again.
8. After a rebase onto a checkout that moved on, the combination is tested again
   before it is published **[enforced]**.
9. One change, measured, then the next: a commit does one thing.

## Approvals

10. Investigate and propose first; change nothing until the person says yes to
    the plan, in so many words. A question is not a yes.
11. Publish and deploy only when the person asks, in the fix request's
    conversation, after the last commit — in words, or a yes to you asking about
    exactly that **[enforced: read from the conversation]**. You cannot approve
    yourself, and a new commit needs a new approval.
12. Say what publishing and deploying will do — which repository, which
    services, whether assistants' turns will be cut — before you ask.

## Data

13. Nothing real leaves in a commit: no credential, no key, no login, e-mail or
    phone from the user store **[enforced: every repository]**, and for this
    stack no household name, address or host **[enforced: the sanitizer]**.
    The check names the kind of value and the file, never the value; keep it so
    in anything you write.
14. What belongs to this house — a setting, an address, a service it does not
    use — goes in the live config (the admin page) or a plugin, never in the
    stack's shipped files.
15. Do not paste household data into the conversation: names, messages, the
    contents of the history or the inbox beyond what the fix needs. The inbox
    is redacted; keep what you quote from it that way.
16. Never read or print a credential. `printenv` a named, non-secret variable;
    never the whole environment.

## Scope

17. The stack's deployer, admin page, manifest, credentials, benchmark and this
    pipeline are not a fix's to change **[enforced]**.
18. Worktrees only: the live checkouts are out of reach **[enforced]**.
