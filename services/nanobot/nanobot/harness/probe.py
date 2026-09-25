"""One short background task on pi, the way a real one runs -- for the Models
page's Test button on the two sub-agent rows.

    python -m nanobot.harness.probe subagent|subagent_powerful

The page's usual test asks the model one question directly, which says the
model answers and nothing about whether it can do a background task: call
pi's tools, read a page, hand back a file. This builds pi's endpoint through
SubagentManager._harness_endpoint -- the same model, provider, Go rule and
refusals a real task meets -- and runs one small task in bench mode, so the
document is stubbed and nothing is filed on the share.

Prints one line, `PROBE_JSON {...}`, whatever happens; logs go to stderr.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

TASK = ("Buscá en la web qué es el pan de masa madre y armame un PDF de una página con: "
        "qué es, tres pasos básicos para hacerlo, y de dónde sacaste cada dato.")
TIMEOUT_S = 180


def _say(doc: dict) -> None:
    print("PROBE_JSON " + json.dumps(doc, ensure_ascii=False), flush=True)


def main(argv: list[str]) -> int:
    role = argv[0] if argv else "subagent"
    if role not in ("subagent", "subagent_powerful"):
        _say({"ok": False, "error": f"not a sub-agent role: {role}"})
        return 0
    try:
        from nanobot.agent.subagent import SubagentManager
        from nanobot.bus.queue import MessageBus
        from nanobot.cli.commands import (_load_runtime_config, _make_powerful_subagent_provider,
                                          _make_provider, _make_subagent_provider)
        from nanobot.harness import pi_runner
        config = _load_runtime_config()
        d = config.agents.defaults
        provider = _make_provider(config)
        sa_provider, sa_model = _make_subagent_provider(config)
        pw_provider, pw_model = _make_powerful_subagent_provider(config)
        work = Path(tempfile.mkdtemp(prefix="pi-probe-"))
        manager = SubagentManager(
            provider=sa_provider or provider, workspace=work, bus=MessageBus(),
            max_tool_result_chars=16000, model=sa_model or d.model,
            powerful_model=pw_model, powerful_provider=pw_provider,
            harness=d.harness, main_model=d.model, main_provider=provider)
        if not (d.harness.enabled and d.harness.engine == "pi"):
            _say({"ok": None, "error": "the pi harness is off: background tasks run on the loop"})
            return 0
        endpoint = manager._harness_endpoint(powerful=role == "subagent_powerful")
        if endpoint is None:
            _say({"ok": False, "error": "pi cannot run this model (not installed, not "
                                        "reachable from pi, or refused) -- such a task runs "
                                        "on nanobot's own loop instead"})
            return 0
        started = time.monotonic()
        res = asyncio.run(pi_runner.run(TASK, endpoint, work / "task", bench=True,
                                        timeout=TIMEOUT_S, want_file=True))
        _say({"ok": bool(res.delivered), "model": endpoint.model, "base": endpoint.base_url,
              "rounds": res.rounds, "turns": res.turns,
              "seconds": round(time.monotonic() - started, 1),
              "tools": [{"name": c.name, "error": c.is_error} for c in res.tools],
              "links": res.links, "error": res.error or "", "reply": (res.text or "")[:500]})
    except Exception as exc:                               # noqa: BLE001
        _say({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
