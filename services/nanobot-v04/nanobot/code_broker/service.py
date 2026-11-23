"""The broker's HTTP surface: verbs for the agent, and who is allowed them.

The agent cannot be trusted to say who it is — that is the entire reason this
process exists — so identity is a token it cannot forge for anyone but itself,
and access is a question only the registry answers.

The chain, in order, for every request:

1. The caller presents a token. It is derived per user from a secret only this
   process holds, so an instance can present its own and nobody else's.
2. The registry is asked what *that named user* may see, using the broker's own
   credential. A project the user cannot see comes back as a 404, the same
   answer a project that does not exist gets.
3. Only then does any git run, in a directory named after that same user.

Nothing in that chain reads a prompt.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web
from loguru import logger

from nanobot.code_broker.skill import envelope
from nanobot.code_broker.workspace import (
    BrokerError, Credential, Workspace, branch, checkout, commit, deploy, merge,
    pull, push, status,
)

_REGISTRY_TIMEOUT_S = 15

def user_token(secret: str, username: str) -> str:
    """The token an instance presents for its own user, and only that one.

    The same shape HomeWeb already uses for its per-user proxy tokens: one
    secret in the deploy, a derived value per instance, and no way to turn one
    instance's token into another's. build-multiuser.sh computes these.
    """
    return hashlib.sha256(f"{secret}:{username}".encode()).hexdigest()


class Broker:
    def __init__(
        self,
        workspace: Workspace,
        portal_url: str,
        broker_token: str,
        client_secret: str,
        users: dict[str, str] | None = None,
    ) -> None:
        self.workspace = workspace
        self.portal_url = portal_url.rstrip("/")
        self.broker_token = broker_token
        self.client_secret = client_secret
        # instance name -> HomeWeb username. Two identifiers exist for one
        # person and each is right in one place: the checkout directory is
        # `user2` because that is what the mounts and the compose call it, and
        # the registry knows `user1` because that is what users.json calls
        # it. Resolving between them belongs here, in the one process allowed
        # to decide who a request is from — and an instance missing from this
        # map is refused before any registry call, so adding a sixth container
        # cannot silently grant it everybody's projects.
        self.users = users or {}

    # --- who is asking ------------------------------------------------------

    def authenticate(self, request: web.Request) -> str:
        """The username this request may act as, or 401.

        Compared with `compare_digest`: the check is cheap and remote, so a
        timing signal is worth denying even though it is a stretch to exploit —
        the same reasoning the debug gate already applies.
        """
        if not self.client_secret:
            raise BrokerError("broker has no CODE_BROKER_SECRET configured", status=503)
        user = (request.headers.get("X-Code-User") or "").strip()
        token = (request.headers.get("X-Code-Token") or "").strip()
        if not user or not token:
            raise BrokerError("unauthorized", status=401)
        if not hmac.compare_digest(token, user_token(self.client_secret, user)):
            logger.warning("code-broker: rejected a token presented for {}", user)
            raise BrokerError("unauthorized", status=401)
        if user not in self.users:
            logger.warning("code-broker: {} is not a known instance", user)
            raise BrokerError("unauthorized", status=401)
        return user

    # --- what the registry says --------------------------------------------

    async def _registry(self, path: str) -> dict:
        url = f"{self.portal_url}/projects/api/broker/{path}"
        try:
            # ssl=False: HomeCore serves its own self-signed cert on the home
            # network, same as every other call to it from here -- the agent's
            # usage report, the homeweb relay and the WhatsApp channel all pass
            # verify=False for this exact reason. This one did not, so *every*
            # git verb failed with "registry unreachable: certificate verify
            # failed" while the broker's own health endpoint went on answering
            # `"ok": true`, because that checks the workspace marker and never
            # dials the registry. Green container, dead feature.
            async with ClientSession(
                    timeout=ClientTimeout(total=_REGISTRY_TIMEOUT_S),
                    connector=TCPConnector(ssl=False)) as http:
                async with http.get(url, headers={"X-Broker-Token": self.broker_token}) as resp:
                    body = await resp.json()
                    if resp.status == 404:
                        raise BrokerError("unknown project", status=404)
                    if resp.status != 200:
                        raise BrokerError(
                            f"registry said {resp.status}: {body.get('error', '')}",
                            status=502,
                        )
                    return body
        except BrokerError:
            raise
        except Exception as exc:
            # The registry being down must not look like a project that does
            # not exist: one is worth retrying and the other never is.
            raise BrokerError(f"registry unreachable: {exc}", status=502)

    # --- verbs --------------------------------------------------------------

    async def handle_list(self, request: web.Request) -> web.Response:
        user = self.authenticate(request)
        body = await self._registry(self.users[user])
        return web.json_response({"projects": body.get("projects", [])})

    async def handle_checkout(self, request: web.Request) -> web.Response:
        user = self.authenticate(request)
        slug = request.match_info["slug"]
        body = await self._registry(f"{self.users[user]}/{slug}")
        project = body["project"]
        result = await checkout(
            self.workspace, user, slug, project["git_url"],
            Credential(body.get("auth_kind", "none"), body.get("auth_secret", ""),
                       body.get("auth_user", "")),
        )
        # Handed back with the project, so the agent learns in the same breath
        # that it is checked out and that merging it will need a person.
        return web.json_response({**result, "project": project})

    async def handle_status(self, request: web.Request) -> web.Response:
        user = self.authenticate(request)
        slug = request.match_info["slug"]
        await self._registry(f"{self.users[user]}/{slug}")   # access, before the disk
        return web.json_response(await status(self.workspace, user, slug))

    # Git, split by what it needs rather than by what it is called. `branch`
    # and `commit` touch nothing but the checkout, so no credential is built
    # for them at all — the agent gets the verbs it needs to do the work
    # without the container ever being near a key. `push` and `pull` talk to a
    # remote, so they go through the same one-subprocess credential every other
    # remote call here uses.
    #
    # All four ask the registry first, and not only for access: `commit` needs
    # the project's protected paths, which is the answer only the registry has.

    async def _body(self, request: web.Request) -> dict:
        try:
            return await request.json()
        except Exception:
            return {}

    async def handle_branch(self, request: web.Request) -> web.Response:
        user = self.authenticate(request)
        slug = request.match_info["slug"]
        await self._registry(f"{self.users[user]}/{slug}")
        name = (await self._body(request)).get("name", "")
        return web.json_response(await branch(self.workspace, user, slug, name))

    async def handle_commit(self, request: web.Request) -> web.Response:
        user = self.authenticate(request)
        slug = request.match_info["slug"]
        body = await self._registry(f"{self.users[user]}/{slug}")
        project = body.get("project") or {}
        message = (await self._body(request)).get("message", "")
        return web.json_response(await commit(
            self.workspace, user, slug, message,
            protected=project.get("protected_paths") or [],
            asked_by=self.users[user],
            author=body.get("display_name", "")))

    async def handle_push(self, request: web.Request) -> web.Response:
        user = self.authenticate(request)
        slug = request.match_info["slug"]
        body = await self._registry(f"{self.users[user]}/{slug}")
        return web.json_response(await push(
            self.workspace, user, slug,
            Credential(body.get("auth_kind", "none"), body.get("auth_secret", ""),
                       body.get("auth_user", ""))))

    async def handle_merge(self, request: web.Request) -> web.Response:
        user = self.authenticate(request)
        slug = request.match_info["slug"]
        body = await self._registry(f"{self.users[user]}/{slug}")
        return web.json_response(await merge(
            self.workspace, user, slug,
            Credential(body.get("auth_kind", "none"), body.get("auth_secret", ""),
                       body.get("auth_user", ""))))

    async def handle_pull(self, request: web.Request) -> web.Response:
        user = self.authenticate(request)
        slug = request.match_info["slug"]
        body = await self._registry(f"{self.users[user]}/{slug}")
        return web.json_response(await pull(
            self.workspace, user, slug,
            Credential(body.get("auth_kind", "none"), body.get("auth_secret", ""),
                       body.get("auth_user", ""))))

    async def handle_deploy_script(self, request: web.Request) -> web.Response:
        """The stored deploy script, as text.

        The agent could write one and never read one back, so after a deploy
        failed it had nothing to go on but its own memory of what it had
        written -- and went on insisting the host was wrong long after the host
        had been fixed, because nothing it could call would tell it otherwise.
        `/v1/projects` says only `has_deploy_script`, deliberately, since a
        listing should not carry every project's whole script.

        Safe to hand over for the reason the whole arrangement exists: a
        credential never appears in this text. It arrives as `$DEPLOY_USER` and
        `$DEPLOY_PASSWORD` in the environment when the script runs, which is why
        the agent may write the script but is never given the password.
        """
        user = self.authenticate(request)
        slug = request.match_info["slug"]
        body = await self._registry(f"{self.users[user]}/{slug}")
        # `body["project"]`, the same place `handle_deploy` reads the script
        # from. The registry nests the row and keeps the credentials beside it,
        # so the script is one level down and reading the top level returns an
        # empty string that looks exactly like a project with no script.
        project = body.get("project") or {}
        return web.json_response({
            "slug": slug,
            "deploy_script": project.get("deploy_script") or "",
            # What the script will be handed when it runs, so the agent can see
            # which account a failed login was for. The name only -- the secret
            # is not here and is not the agent's to see.
            "deploy_user": body.get("deploy_auth_user") or "",
            "deploy_auth_kind": body.get("deploy_auth_kind") or "none",
            "deploy_path": project.get("deploy_path") or "",
        })

    async def handle_deploy(self, request: web.Request) -> web.Response:
        """Run the project's deploy script with its credential in hand.

        The credential is fetched here, from the registry, on this request --
        never taken from the body. That is the same rule every other verb
        follows and it matters most here: the body is written by the agent, and
        a caller that could name its own secret could name somebody else's.
        """
        user = self.authenticate(request)
        slug = request.match_info["slug"]
        body = await self._registry(f"{self.users[user]}/{slug}")
        # The *deploy* credential, not the repository's. They are different
        # fields for a reason: one opens the source, the other authenticates to
        # wherever the project is being put, and a deploy script has no
        # business holding the first.
        return web.json_response(await deploy(
            self.workspace, user, slug, body["project"],
            Credential(body.get("deploy_auth_kind", "none"),
                       body.get("deploy_auth_secret", ""),
                       body.get("deploy_auth_user", ""))))

    async def handle_skill(self, request: web.Request) -> web.Response:
        """The instructions for this API, served by the API.

        Deliberately unauthenticated: it is a description of a contract, the
        same one anybody with the source can read, and it names no project and
        no person. Gating it would only mean the agent needs a credential to
        learn how to ask for one.
        """
        return web.json_response(envelope())

    async def handle_health(self, request: web.Request) -> web.Response:
        marked = (self.workspace.root / ".nanobot-code-workspace").is_file()
        return web.json_response({
            "ok": marked,
            "workspace": str(self.workspace.root),
            "workspace_marked": marked,
            "registry": self.portal_url,
        })


@web.middleware
async def _errors(request: web.Request, handler):
    """A BrokerError is an answer; anything else is a bug and says so once."""
    try:
        return await handler(request)
    except BrokerError as exc:
        return web.json_response({"error": str(exc)}, status=exc.status)
    except web.HTTPException as exc:
        # aiohttp raises these to *mean* a status -- an unrouted path arrives
        # here as HTTPNotFound. Swallowed by the catch-all below, every unknown
        # URL answered 500 with "internal error: HTTPNotFound", which reads as
        # the broker having broken rather than as there being no such verb.
        # Found by removing two routes and checking they 404: they did not.
        return web.json_response(
            {"error": exc.reason or exc.__class__.__name__}, status=exc.status)
    except Exception as exc:
        logger.exception("code-broker: unhandled error on {}", request.path)
        return web.json_response({"error": f"internal error: {type(exc).__name__}"}, status=500)


def create_app(broker: Broker) -> web.Application:
    app = web.Application(middlewares=[_errors])
    app.router.add_get("/health", broker.handle_health)
    app.router.add_get("/skill", broker.handle_skill)
    app.router.add_get("/v1/projects", broker.handle_list)
    app.router.add_post("/v1/projects/{slug}/checkout", broker.handle_checkout)
    app.router.add_get("/v1/projects/{slug}/status", broker.handle_status)
    app.router.add_post("/v1/projects/{slug}/branch", broker.handle_branch)
    app.router.add_post("/v1/projects/{slug}/commit", broker.handle_commit)
    app.router.add_post("/v1/projects/{slug}/push", broker.handle_push)
    app.router.add_post("/v1/projects/{slug}/merge", broker.handle_merge)
    app.router.add_post("/v1/projects/{slug}/pull", broker.handle_pull)
    app.router.add_get("/v1/projects/{slug}/deploy-script",
                       broker.handle_deploy_script)
    app.router.add_post("/v1/projects/{slug}/deploy", broker.handle_deploy)
    return app


def parse_users(raw: str) -> dict[str, str]:
    """`user2:user1,user3:user2` -> a mapping.

    Malformed entries are dropped with a line in the log rather than crashing
    the service: a typo in one person's entry should cost that person the
    feature, not the house the broker.
    """
    users: dict[str, str] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        instance, _, username = entry.partition(":")
        if instance.strip() and username.strip():
            users[instance.strip()] = username.strip()
        else:
            logger.warning("code-broker: ignoring malformed CODE_BROKER_USERS entry {!r}", entry)
    return users


def registry_url() -> str:
    """Where the project registry lives.

    A named function rather than an inline `os.environ.get`, so what the broker
    resolved can be logged and tested instead of re-derived by whoever reads
    this next. Upstream arrived here the expensive way: its copy read
    HOMEWEB_URL while the compose set HOMECORE_URL, so the value an operator
    configured was ignored in favour of a hardcoded host that resolves nowhere
    from inside a container -- and nothing in the log said which one won.

    That half cannot happen here. The rename landed at extraction and
    `deploy/manifest.yml` supplies HOMECORE_URL from HOMECORE_CONTAINER_URL,
    which --check-contract asserts is exported. The half that would still bite
    is kept: there is deliberately no default, because a wrong address is worse
    than none, and an unset one announces itself at startup instead of
    surfacing later as an unexplained `registry unreachable`.
    """
    return (os.environ.get("HOMECORE_URL") or "").strip()


def build_from_env() -> web.Application:
    workspace = Workspace(os.environ.get("CODE_WORKSPACE_DIR", "/nanobot-code-workspace"))
    users = parse_users(os.environ.get("CODE_BROKER_USERS", ""))
    broker = Broker(
        workspace=workspace,
        portal_url=registry_url(),
        broker_token=os.environ.get("PROJECTS_BROKER_TOKEN", ""),
        client_secret=os.environ.get("CODE_BROKER_SECRET", ""),
        users=users,
    )
    logger.info("code-broker: serving {} instance(s): {}", len(users), ", ".join(sorted(users)))
    if broker.portal_url:
        logger.info("code-broker: registry at {}", broker.portal_url)
    else:
        logger.warning("code-broker: HOMECORE_URL unset -- every registry call "
                       "will fail on the URL rather than on the network")
    if not broker.broker_token:
        logger.warning("code-broker: PROJECTS_BROKER_TOKEN unset — the registry will refuse")
    if not broker.client_secret:
        logger.warning("code-broker: CODE_BROKER_SECRET unset — every request will be refused")
    return create_app(broker)
