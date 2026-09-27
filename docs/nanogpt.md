# NanoGPT

[nano-gpt.com](https://nano-gpt.com) is an OpenAI-compatible gateway: hundreds of
models under `vendor/model` ids behind one key. This stack treats it as one more
provider beside OpenCode Zen, OpenRouter and Together, with one thing none of
those have -- a flat subscription that covers a large part of its roster. That is
what makes it worth having: the open models this stack runs best (DeepSeek,
Kimi, GLM, MiniMax, Qwen) are included in it.

## Setting it up

1. Create an **API key** at nano-gpt.com/api. It looks like `sk-nano-` followed by
   a UUID, about 44 characters. Two other things NanoGPT hands out are not it,
   and both fail as `401 Invalid session`: a *usage-only* management token
   (it cannot run models, by design) and an account's login code (a formatted
   `xx-xxxx-…` string -- and a credential for the whole account, which does not
   belong in this stack at all).
2. Put it in `NANOGPT_API_KEY` on the admin page's **Secrets** (model
   credentials). Setting the key is the switch: the Models page lists the
   roster, and the deployer refuses a `nanogpt:` model while the key is empty.
3. Choose models on the **Models** page, or write them in `assistant.models` as
   `nanogpt:<vendor/model>`, e.g. `nanogpt:deepseek/deepseek-v4.1-flash`. A
   suffix NanoGPT uses is part of the name: `nanogpt:qwen/qwen3.8-27b:thinking`.
4. Deploy `nanobot` and `nanobot-house`. Both assistants declare the provider;
   so do the chat titler and Paperless, which read the same key.

## Subscription or pay per token

The subscription covers roughly half the roster (296 of 622 models on
2026-09-26) with a weekly input-token allowance (60 M at the time of writing);
the rest is billed per token from the account balance. The Models page lists the
two apart -- **NanoGPT — subscription** and **NanoGPT — pay per token** -- in every
role's picker and in the catalogue's provider filter, because the same price
column means a bill for one and nothing for the other.

A pay-per-token model on an account with no balance answers **`HTTP 402`**. On
2026-09-26 every GPT-6 model was in that group: moving `everyday` and the
professions to `nanogpt:openai/gpt-6-*` would have failed every turn. Keep the
roles on covered models, or fund the balance on purpose.

What is covered is NanoGPT's decision and changes without notice; the Models
page reads it from `/api/v1/models?detailed=true` on every refresh.

## Things this provider does differently

- **Thinking levels are per model.** NanoGPT refuses a `reasoning_effort` a
  model does not take and names the ones it does (`xiaomi/mimo-v2.6-flash`:
  none and high only). The assistant retries once at the nearest accepted level
  -- the higher one on a tie, so a role that asked for some thinking does not
  silently get none -- and remembers it for that model. Before that, the
  planner (which carries the everyday level, `low`) failed every multi-step
  request on MiMo.
- **Occasional malformed tool calls.** NanoGPT sometimes answers
  `502 Upstream emitted malformed tool call data`. It is retried like any 5xx,
  and when the model repeats it the turn goes to the fallback chain -- which is
  why **the fallback must stay on another provider** than `everyday`.
- **The room assistant runs the everyday model**, so `nanobot-house` declares
  NanoGPT too. The deployer refuses any role on a provider an assistant's config
  does not declare, rather than shipping a room speaker that fails every turn.

## Recommended models

Measured on this stack's own benchmark (the Models page's **Test**, or
`Benchmark and prices`) on 2026-09-26/27, all on the subscription. The
benchmark is the house's cases, not a general ranking: re-test before trusting
a result on a different setup.

| Role | Model | Result | Notes |
|---|---|---|---|
| `everyday` | `deepseek/deepseek-v4.1-flash` | 13/13 everyday + tools | Reads images, 1 M window |
| `powerful`, `teacher`, `doctor`, `legal` | `deepseek/deepseek-v4-pro` | 12/13 | Accepts only none/high/max thinking |
| `designer` | `qwen/qwen3.5-397b-a17b` | probe only | The largest covered model that reads images |
| `programmer` | `moonshotai/kimi-k2.7-code` | probe only | The same model OpenCode Zen lists but did not route (404) |
| `subagent` | `deepseek/deepseek-v4-flash` | probe only | `…-latest` is an alias NanoGPT can repoint |
| `planner` | `z-ai/glm-5.3` or `minimax/minimax-m3` | 3/5 | The best any model has scored here, GPT-6 on Zen included |
| `plan_steps` | keep a local model | — | `qwen3.5:9b` scored 5/6; `deepseek-v4-flash` on NanoGPT 4/6 and slower |

Planner, the full comparison (five cases; the hard ones time out for every
model, so 3/5 is the ceiling so far):

| Model | Planner |
|---|---|
| `z-ai/glm-5.3` | 3/5 |
| `minimax/minimax-m3` | 3/5 |
| `deepseek/deepseek-v4-flash` | 2/5 |
| `deepseek/deepseek-v4.1-flash` | 2/5 |
| `deepseek/deepseek-v4-pro` | 1/5 |
| `moonshotai/kimi-k2.7-code` | 1/5 |
| `xiaomi/mimo-v2.6-flash` | 0/5 |
| `qwen/qwen3.8-flash` | 0/4 (stopped) |

The local roles -- notifications, events, heartbeat, classifier, titles,
documents, vision -- have no reason to move: they cost nothing and never leave
the house.
