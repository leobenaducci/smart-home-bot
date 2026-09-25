# Everything optional

The stack is fully usable with nothing but a LAN and one model API key. Four
things can reach outside it, and all four are off or local by default. Turn
them on from the admin page (*Site → Outside connections*) or in
`config/home-stack.yml` under `cloud:`.

## The model API key — the one that is required

`OPENCODE_API_KEY` in `secrets/smart-home-bot.env`. Without it the assistant
containers start and answer `/health`, and every actual turn fails. There is no
local fallback: a household assistant running on a model small enough to serve
from the hub is not the same product.

> **Use the Zen endpoint. Go belongs to the Programmer alone.** One account and
> one key open both: `https://opencode.ai/zen/v1` is Zen, billed per token, and
> `https://opencode.ai/zen/go/v1` is Go, the flat $10/month subscription.
> Everything configured here points at the first and must not be pointed at the
> second. Go is spent by exactly one thing: `opencode serve`, the Programmer's
> harness — OpenCode's own client, answering a person at a keyboard, holding its
> own credential. That is the case the flat plan is sold for; the rest of this
> stack is not it. OpenCode's own documentation says Go traffic is "monitored
> for abusive traffic that degrades the experience for other users" and that a
> client has to "properly identify itself" rather than use a broad user agent.
> What this stack sends is not an interactive coding session: five containers
> on one key, running unattended around the clock -- notification triage every
> few minutes, cron reminders, memory consolidation, a morning summary -- on
> the order of 1,800 runs a month with nobody at a keyboard. That is the
> traffic shape a flat plan flags, and **the account can be blocked for it**.
> Zen carries the wider roster anyway: 94 models against Go's 33.

Everything else below is genuinely optional.

## Notifications — `cloud.notifications.mode`

| Mode | What happens |
|---|---|
| `local` *(default)* | An ntfy container on the hub. Notifications reach phones on the LAN or over your VPN. Nothing leaves the network, and no credential is needed. |
| `external` | Points at an ntfy server you host elsewhere. Needs `NTFY_CREDENTIALS` as `user:password` — it is used as HTTP basic auth, not as a bearer token. |
| `off` | No notifications. Tasks, cameras and the assistant all still work; they simply do not push. |

Per-person topics default to the member id, so `home-core`, the assistant's
`ntfy-send` and the MQTT dashboard's bridges all agree without configuration --
and `members[].ntfy_topic` overrides it, which is what a household with an
existing ntfy wants: their phones are subscribed to a person's name, not to
`user1`. The deployer writes whichever applies into `NTFY_TOPIC_<MEMBER>`.

Which of the three ids that is matters, and the answer here is the member id
rather than the login or the folder — see the table in CLAUDE.md. A topic is
not reached from a request; it is built by the deployer, which is the half the
member id is for.

## Certificates — `cloud.certificates.mode`

| Mode | What happens |
|---|---|
| `local-ca` *(default)* | The proxy generates a CA and issues for your internal names. No registrar, no ACME, no outbound connection. You install the CA on the household's devices once. |
| `acme-dns` | A real certificate over a DNS API. Needs a domain you own, `cloud.certificates.acme_dns_provider` set to your provider's acme.sh plugin name, and `ACME_DNS_API_TOKEN`. |

`local-ca` is the default because an outage at a registrar should not be able
to take the house's own chat down with it.

Whichever mode is set, the deploy verifies with `--resolve` against the trust
store rather than with `-k`. The question worth asking is whether a client that
trusts the right CA accepts what you serve for that name, and `-k` answers no
question at all.

## The public proxy — `cloud.vps.enabled`

The only part of this stack meant to run anywhere but your own machine, and it
is **a proxy and nothing else**. It holds no application code and no household
data. Every request it accepts is forwarded down a reverse tunnel to the machine
at home, which serves the page, holds the user store and checks the password.

Off by default. Turning it on needs `cloud.vps.host`, `cloud.vps.user`, and ssh access.
The trust secret is `PROXY_SHARED_SECRET`, the same one the portal already
holds -- there is no separate VPS credential.

`cloud.vps.serve_entry_page` decides whether the proxy also forwards the entry
page, not whether a copy of it runs out there. There is no copy.

Logins are verified at home: the proxy posts the credentials to the portal's
`/api/auth/verify` over the tunnel and believes the answer, and an unreachable
upstream is a failed login rather than a fallback. `AUTH_MODE=local` is
available for an install that deliberately wants the proxy to keep working
while home is unreachable — at the cost of a copy of the household's bcrypt
hashes on hardware they do not control.

The LAN copy of the same proxy (`local-proxy`) runs regardless and is what the
household reaches from inside. Leaving the VPS off costs you nothing but
off-network access.

If you enable it, note the split horizon: your internal DNS must answer the
public name with the home machine, and that machine must pin the real external
address in its own `/etc/hosts`, or the deploy loops back on itself.

## Assistant extras

Each is a feature that is simply off when its key is empty — an absent key
never fails a deploy.

| Key | Enables |
|---|---|
| `HOMEASSISTANT_TOKEN` | Home Assistant control, if you run one locally |
| `PAPERLESS_API_TOKEN_*` | Document search, one token per member |
| `TAVILY_API_KEY` / `BRAVE_API_KEY` / `KAGI_API_KEY` / `JINA_API_KEY` | Web search. Set at most one. |
| `NANOBOT_N8N_API_KEY` | Workflow automation, if you run n8n locally |

Home Assistant, Paperless and n8n are all things you host yourself; only the
search providers are external services.
