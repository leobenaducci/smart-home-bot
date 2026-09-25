# Running each assistant instance under Kubernetes

**Status:** on the `kubernetes-nanobot` branch. Running: one member is up under
k3s on the test VPS, `1/1 Running`, `/health` answering, identity correct.

What it took to get there is worth keeping, because none of it was visible from
reading the compose file:

| symptom | cause |
|---|---|
| pod exits 0, "Completed", no error anywhere | the image is `ENTRYPOINT ["entrypoint.sh"]` + `CMD ["status"]`. Compose's `entrypoint:` key *clears* the image CMD; Kubernetes' `command:` does not, and an empty `args: []` is dropped on the way in. It ran `entrypoint.sh status`, printed, and exited. |
| `/home/nanobot/.nanobot is not writable` | the image runs as uid 1000. Compose chowns with an init container; k8s needs `fsGroup`. |
| five "missing under /etc/nanobot" warnings | the first render carried the environment and not the assistant's own prompt files. Compose bind-mounts `config/`; k8s needs it as a ConfigMap. |
| `NANOBOT_API_SECRET ... is not set` | `member.upper()` gives `USER1`; the deployer's own tested mapping gives `USER_1`. Rolling it again by hand rendered a Secret full of empty values. |
| then `TOGETHER_API_KEY ... is not set` | omitting empty secrets. `${VAR:-}` in compose *sets the variable to empty*; the container distinguishes unset from empty and refuses to start on the first thing its config references that is unset. |
| fixes not taking effect | `kubectl apply` does not recreate a pod stuck in CrashLoopBackOff. Three fixes never reached the running pod. |

Two more after that, both found by looking at the running pod rather than the
YAML: every host-facing address was `host.docker.internal`, which is Docker's
name for the machine and resolves nowhere in a pod -- so the assistant came up
unable to reach Ollama, the tasks API or ntfy, with nothing in its logs saying
so. They come from the node's own address through the downward API now, and
have to be `env:` entries rather than ConfigMap keys, because `$(VAR)`
expansion works in `env` and `envFrom` expands nothing. And `render` wrote a
warning to stdout, in the middle of the YAML a person redirects to a file.

The PVC was also rendered and never mounted -- an object nothing used, `Pending`
forever, which is the decorative-declaration shape this deployer has been bitten
by twice before.

**The probe was then demonstrated, not assumed.** Pointing liveness at a port
nothing serves produced `Liveness probe failed ... Container nanobot failed
liveness probe, will be restarted` and a restart count of 1, then the real
manifest was re-applied and the pod came back healthy on 8900. Three attempts
to wedge the process itself failed first -- `kill -9 1` is a no-op inside a PID
namespace, and the server survived both `pkill` and `kill -STOP -1` -- which is
worth knowing: this instance is harder to hang than the case that motivated
the work.

**Why:** one assistant per household member, and today nothing watches them.
A member's instance can be wedged for a day and the only signal is that person
saying it stopped answering. And every instance runs on whichever machine
`hosts.hub` points at, so the household's compute is one box whether or not it
has more.

Kubernetes buys exactly two things here, and it is worth being precise about
which, because they are the whole justification: **a probe that restarts a
wedged instance without anybody noticing it was wedged**, and **a scheduler
that puts an instance on a machine with room for it**.

---

## What an instance is today

Grounded in `services/nanobot/docker-compose.multiuser.yml` and
`deploy/manifest.yml`, not from memory:

| | |
|---|---|
| Image | `alfred-nanobot:latest`, built on the target, **in no registry** |
| Replicas | one per member, gated by a compose profile, **rendered** from `services.nanobot.members` by `deploy/compose_members.py` |
| Identity | `NANOBOT_INSTANCE`, `HOMECORE_USER_ID`, `FILE_SHARE_FOLDER` |
| Credentials | 11 secrets, 4 of them per-member (`*_USER_N`) |
| State | `{paths.state}/nanobot/<member>` → `/home/nanobot/.nanobot` |
| Config | `{paths.config}/nanobot` read-only, plus `ntfy-send` on PATH |
| Ports | gateway `876N`, API `890N`, WhatsApp bridge `1879N` |
| Health | **none.** `restart: unless-stopped` and nothing else |
| Deploy verify | `GET /health` → `{"status": "ok"}`, 180s |

That last pair is the case for doing this. `/health` exists and answers; no
container is configured to ask it. A process that is up but not answering is
indistinguishable from one that is fine.

---

## What has to be true first

### 1. A registry, or there is no second machine

`alfred-nanobot:latest` is built where it is deployed. Kubernetes schedules a
pod onto a node and that node pulls the image — so the moment there is a second
node, "built locally" means "runs on one node only", and the scheduler quietly
becomes a no-op that always picks the machine that happens to have the image.

This is the blocker for the distribution half, and it is not optional. Either a
registry in the house (`registry:2` behind the existing proxy, one more service
in the manifest) or `imagePullPolicy: Never` plus a build on every node, which
is not a design so much as a way of pretending the problem is solved.

### 2. Two pods must never write one member's state

`.nanobot` holds consolidated memory — `USER.md` is written by the
consolidation pass, not just read. Two pods on it corrupts a person's assistant
memory, and the corruption is silent and cumulative.

So: `strategy: Recreate`, never `RollingUpdate`, and an `RWO` volume. The
default rolling update starts the new pod before stopping the old one, which is
exactly the overlap that must not happen. This is the single most dangerous
detail in the migration.

### 3. Identity has to be derived, not written five times

**Done, and it turned out to be the compose side that needed it.**

Three of the five instances shipped answering as the wrong member —
`nanobot-user1` sent `HOMECORE_USER_ID=user3` while holding user1's tokens —
because the identity was a literal repeated per service block. This was written
as a warning about generated manifests: five near-identical YAML documents is
precisely the shape that invites a copy-paste nothing catches.

The warning was right and pointed the wrong way. `deploy/kubernetes.py` has
derived all of it from one member id since it was written. The *compose* file
was the one with five hand-written blocks, and by the time anyone looked they
had drifted in four more ways — one member with no `TASKS_API_URL`, the
file-share admin flag on the wrong two people, every instance told its share
folder was `userN` when the share has no such directory, and WhatsApp wired to
one id by literal.

Both runtimes render from the member list now (`deploy/compose_members.py` for
compose), and `deploy/test_deploy.py` asserts the derivation against the
rendered output rather than against a file in the tree. That also removes the
quiet half of this whole plan's cost: a change to how an instance is built is
one edit in each renderer, not one edit and five copies.

---

## The shape

One `StatefulSet` per member rather than one with five replicas. Members are
not interchangeable ordinals — `user2` is a person, not replica 1 — and a
shared StatefulSet ties their lifecycles together for no benefit.

```
nanobot-<member>          StatefulSet, replicas: 1, strategy: Recreate
  volumeClaimTemplates    RWO PVC -> /home/nanobot/.nanobot
  envFrom                 Secret nanobot-<member>   (that member's 4 keys)
  envFrom                 Secret nanobot-shared     (the 7 house-wide keys)
  configMap               nanobot-config (read-only)
  readinessProbe          GET :8900/health, expect status ok
  livenessProbe           the same, failureThreshold high enough for a
                          180-second cold start -- the deploy verify already
                          allows that, and a liveness probe that kills a slow
                          start is a restart loop that looks like a crash
nanobot-<member>          Service, ClusterIP, the three ports
```

**Secrets split in two on purpose.** The house instance holds no per-member
credentials — asserted today by a `container_env_absent` check — and that
boundary has to survive. Per-member keys go in a per-member Secret; nothing
else mounts it.

**Probes are not the deploy verify.** `/health` says the process is up.
`manifest.yml` asks the assistant a real question and reads the answer, which
is what caught the dead API key on the VPS. Keep both: the probe restarts a
wedged pod, the verify refuses to call a deploy done.

---

## Where the manifests come from

Not hand-written, and not a second source of truth. `deploy/manifest.yml`
already describes state paths, secrets, ports and per-member expansion, and
`deploy.py` already interpolates `{services.nanobot.api_port_base}` and friends.
The k8s documents should be **rendered from the same manifest**, so that
adding a member stays one line in `config/home-stack.yml`.

That argues for a `kind: kubernetes` unit type alongside the compose one, and
`--check-contract` growing an equivalent: every `${VAR}` a rendered document
references must be supplied, and a state path must still not resolve inside
anything a deploy replaces.

---

## Migration

State is host directories today and becomes PVCs. That is a copy, and it is the
step where a household loses assistant memory if it is done casually.

The backup tool now does exactly this job — `./home-stack backup` then verify —
so the migration is: back up, verify the backup restores, stop the instance,
copy `{state}/nanobot/<member>` into the PVC, start, ask the assistant something
only it would know. Per member, one at a time, reversible at every step.

---

## What does not move

`home-core`, the proxies, the cameras, MQTT and Paperless stay on compose.
Nothing here argues for moving them, and a second deployment mechanism is a
cost paid per service. The stack's stated default is still one PC; this makes
one *service* able to spread, for the household that has more than one machine.

---

## Decided

**Compose stays; Kubernetes is opt-in, per service.**

```yaml
services:
  nanobot:
    runtime: compose        # or: kubernetes
```

The single-PC install is unchanged and needs no cluster, so "clone and run on
one PC" stays true. The cost is real and worth writing down: every change to
how nanobot is deployed now lands in two places, and the opt-in path is the one
nobody exercises until it breaks. That argues for the rendered manifests being
generated from the same `deploy/manifest.yml` rather than maintained beside it,
and for the identity assertions running against the rendered output whether or
not anybody has opted in.

**The installer asks.** k3s is not stood up behind anybody's back, and not
assumed either. `./home-stack install` gains a question in the same shape as
the assistant one:

```
    The assistant runs one instance per member. Kubernetes can watch them
    and restart one that stops answering, and spread them across machines.

      1  No -- Docker Compose on this machine        (default)
      2  Install k3s here and use it
      3  I already have a cluster -- use my kubeconfig
```

Answer 1 and nothing about this exists. Answer 2 and the installer stands up
k3s and the in-house registry. Answer 3 and it asks for a kubeconfig path and a
namespace, and verifies it can reach the cluster before writing anything.

**Still open, and each follows from the above rather than blocking it:**

- **Backups.** PVC contents are not host paths, so the tool would need
  `kubectl exec` or a volume snapshot. This makes the review's open finding --
  that backup assumes every path is local -- urgent rather than theoretical,
  and it should be fixed before anyone runs a member on a second machine.
- **`nanobot-house`.** Same shape, no per-member state. Gets the same
  `runtime:` switch; no reason to treat it differently.

## Phases

0. ~~Identity derived from one member id on both sides~~ — done; the compose
   half was the one missing it.
1. Registry in the manifest; `alfred-nanobot` pushed to it. Nothing else changes,
   and it is useful on its own.
2. Render for one member, run it alongside compose on one node, compare.
3. Identity assertions on the rendered output before a second member exists.
4. Migrate members one at a time, backup-verified each time.
5. Second node, and only then is any of this worth having.
