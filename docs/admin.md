# The admin page

`http://<hub>:21002/` — settings for the whole stack: what runs, where, who
lives here, which credentials exist, and deploys.

It is published on every interface, so it answers on the household's own
network. That is who it is for; a page you can only reach by shelling into the
hub is not one anybody uses.

**What keeps it off the internet is that neither proxy routes to it.** The
proxies answer from outside on purpose, so one `reverse_proxy` line to 21002
would hand the docker socket, the secrets file and the Deploy button to
everyone — with `bind` still reading `0.0.0.0` and looking untouched.
`./home-stack check` refuses a tree where anything routes
to the admin port, and `deploy/test_deploy.py` asserts it without needing a
config.

**On a machine with a public address this does not hold** — a VPS, or a host
your router forwards to. There `0.0.0.0` is the internet, and nothing in this
package can tell from the inside. On one of those:

```yaml
services:
  admin:
    bind: 127.0.0.1
```

```bash
ssh -L 21002:127.0.0.1:21002 <host>   # then http://localhost:21002/
```

The deploy prints which of the two you are on every time it touches this
service.

Two addresses share the name and they are not the same setting. `bind` is the
host side — who may reach the page. Inside the container the app listens on
`0.0.0.0`, meaning the container's own interfaces, which is the only address a
published port can forward to; binding *that* to loopback makes the page
unreachable while the healthcheck, which runs inside, goes on reporting
healthy.

**The page it opens on says what is left to do.** A missing model API key, a
household with nobody in it, and settings saved here but not yet deployed each
appear on the overview with a link to the page that fixes them — so "saving is
not deploying" is visible rather than something you have to remember.

## What it will not do

**It never shows a secret back.** Values are write-only. Once a key is set the
page reports only whether it is present. A settings page that renders your API
keys is a settings page that leaks them to whoever walks past the screen it is
open on.

**It never deploys as a side effect of saving.** Saving writes the deployed
`{paths.config}/home-stack.yml` — the copy the page owns, seeded from the
checkout on the first deploy and never re-seeded after; a change is saved and
waiting until you press Deploy.
Surprise deploys are how a house loses its lights at dinner time.

## Pages

| Page | What it edits |
|---|---|
| Overview | Nothing. Host roles and how many services are on. |
| Services | `enabled` per service. Off means the deployer skips it and the portal hides its tile — it does not remove anything already running. |
| Site | Name, internal domain, time zone, languages, and the optional outside connections. |
| Household | Who lives here. Adding a person allocates an assistant instance; the two lists are kept in sync so the deployer never allocates ports for someone who does not exist. |
| Credentials | Writes the deployed `{paths.config}/smart-home-bot.env`, not the checkout copy it was seeded from. |
| Deploy | Runs `deploy/deploy.py`, with a preview that maps to `--dry-run`. |

## The portal's user store

Separate from members, and separate from this page. `{state}/home-core/users.json`
holds `{"username", "hash", "nanobot_id"}` per login: `username` is what a person
types, `hash` is bcrypt, and `nanobot_id` is what ties that login to a member's
assistant instance.

The package ships it empty and there was no way to write the first entry — the
portal has no sign-up, and this page manages members rather than logins. That is
the gap `--create-user` fills. Members are still added here; a *login* for one
is the installer's job.

## Member ids

Generated and opaque — `user1`, `user2`. Never a national identity number.

The stack this package came from used real national id numbers as portal login
names, which meant the user store was a list of identity numbers next to
password hashes. The admin page will not create one, and nothing in the package
reads one.

## Getting in

The page asks for a password, and there is no default one.

`./home-stack install` asks for it, along with the first login for the household
portal — two different passwords, because they guard two different things and
one value shared between them would quietly widen the second. The admin page
can deploy and can read every credential in the house; a portal login is one
person's own account.

Neither is generated. A password nobody typed lives in a terminal scrollback
and in the password manager of whoever ran the installer, and these are the
accounts the household actually uses.

If the installer ran without a terminal — piped, or in CI — it asks nothing and
says so. Then:

- **the admin page** falls back to a first-run setup screen: until a hash
  exists, nothing else on the page answers, so it cannot sit open waiting for
  somebody to notice;
- **the portal user** is created by `./home-stack install --create-user`, which
  is also how you add the first login to an install that predates this.

The password is stored as a bcrypt hash in `ADMIN_PASSWORD_HASH`, in the same
env file the page already edits — the installer writes the same hash the page
verifies, so it makes no difference which one set it. So:

- **Changing it takes effect immediately**, not on the next deploy — and it
  signs every other browser out, which is the point of changing it after a
  laptop goes missing.
- **There is no recovery.** Set a new one from the page, or, if you are locked
  out entirely, delete the `ADMIN_PASSWORD_HASH` line from the file the
  container actually reads — `{paths.config}/smart-home-bot.env`, by default
  `/var/lib/home-stack/config/smart-home-bot.env` — and reload: you are back at
  the setup screen. Editing the checkout's `secrets/smart-home-bot.env` instead
  changes nothing the running page reads; that copy is only the seed. Anyone
  who can do either already has the file the password protects, so this costs
  nothing.
- **`/healthz` answers without a session**, so a container healthcheck needs no
  credential. It returns nothing but `{"status": "ok"}`.

Five wrong guesses from one address and that address waits fifteen minutes.
bcrypt already makes guessing slow; this turns a long run of it into a wait
rather than a rate.

## Deploying from this page, on a one-machine install

It refuses, and that is deliberate. The page runs the deployer as a subprocess
of itself, inside its own container, and on the default install every role is
`127.0.0.1` — which inside a container is the container.

Three things go wrong quietly if it does not refuse. A verify `curl` reaches
this container instead of the machine (measured: the portal answers 200 on the
host and nothing at all in here). State directories are created inside the
container while Docker auto-creates root-owned ones on the host. A `pre:` script
writes where nobody will look. Containers really do start, because the Docker
socket is mounted — so the failure is not "nothing happened", it is that the
work lands on one machine and the checking on another.

Two ways to deploy:

- **From a shell on the host**: `./home-stack deploy`. This is the supported
  path on a single machine, and the page still does everything else — settings,
  members, secrets, and telling you which services are waiting for a deploy.
- **Give the roles real addresses.** Then the deployer connects over ssh with
  the keys this container already mounts at `/root/.ssh`, and the button works
  as intended. That is the arrangement it was built for.

`HOME_STACK_ALLOW_CONTAINER_LOCAL=1` overrides the refusal. Only set it if the
container genuinely shares the host's network namespace *and* its paths;
otherwise it turns a clear error back into three silent ones.

**A deploy from this page ships the page's own copy of the code**, the tree
staged into the admin container the last time *admin* was deployed -- not the
checkout. After shipping services from a shell, deploy `admin` too, or the next
deploy started here puts them back on older code without a word (measured
2026-09-25: a six-service deploy from the page undid a nanobot change made
twenty minutes earlier). Two deploys at once also race on the same image tags,
so check nothing else is deploying first.

## Exposure

This is the one service that can change what every other service does. Treat
access to it as equivalent to root on every host in `config/home-stack.yml`.

- It is never placed behind the public proxy, and its compose file offers no
  way to do that.
- The password is what stands between the page and whoever can reach the port.
  It used to be assumed that the LAN did that job — an assumption the app
  cannot check, and one that is simply false the moment the port is published
  on a host with a public address.
- Binding it to `127.0.0.1` and reaching it over ssh is still the strongest
  arrangement, and the password is what makes anything less than that
  survivable.
