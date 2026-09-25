"""Drawing a picture that no catalogue holds.

The Designer could search for photographs and hand-write SVG, and nothing else
— so what it made was type on a flat field. The household already pays for an
image model (`assistant.models.image_normal` / `image_high`), but only the
`theme` skill read it, which wired Imagen to wallpapers and nowhere else.

Most of what is pinned here is the download. A drawn picture cannot go through
`get`: that path's allowlist exists because *the model* chose the URL, and
Together's CDN is not on it, so every drawing would be refused. The URL here
comes from Together's own reply instead — which means the allowlist is the
wrong guard and `validate_url_target` is the right one, and nothing may reach
the house.
"""

import importlib.util
import json
import pathlib

import pytest

_PATH = (pathlib.Path(__file__).resolve().parents[2]
         / "nanobot" / "skills" / "images" / "find_image.py")
_spec = importlib.util.spec_from_file_location("find_image", _PATH)
fi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fi)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(fi, "TOGETHER_KEY", "a-key")
    monkeypatch.setattr(fi, "WORKSPACE", tmp_path)
    monkeypatch.setattr(fi, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setenv("TOGETHER_IMAGE_MODEL", "google/imagen-4.0-preview")
    monkeypatch.setenv("TOGETHER_IMAGE_MODEL_HIGH", "google/imagen-4.0-fast")
    # The house's own model, if this box happens to have one configured, would
    # otherwise win every one of these and answer a different question.
    for _k in ("IMAGE_API_MODEL", "IMAGE_API_MODEL_HIGH", "IMAGE_API_URL",
               "IMAGE_API_URL_HIGH", "IMAGE_API_KEY"):
        monkeypatch.delenv(_k, raising=False)


class TestWhichModel:
    """`_draw_endpoint` returns the whole (model, url, key) triple, because
    picking a model from one role and the URL from a module constant is how
    "high quality" quietly started meaning "the local model, addressed as
    Together"."""

    def test_high_by_default_because_somebody_will_look_at_it(self):
        assert fi._draw_endpoint("high")[0] == "google/imagen-4.0-fast"

    def test_normal_when_it_is_incidental(self):
        assert fi._draw_endpoint("normal")[0] == "google/imagen-4.0-preview"

    def test_it_falls_back_rather_than_refusing(self, monkeypatch):
        monkeypatch.delenv("TOGETHER_IMAGE_MODEL_HIGH")
        assert fi._draw_endpoint("high")[0] == "google/imagen-4.0-preview"

    def test_a_hosted_role_keeps_togethers_endpoint_and_key(self):
        model, url, key = fi._draw_endpoint("high")
        assert (url, key) == (fi.TOGETHER_URL, "a-key")

    def test_a_self_hosted_model_brings_its_own_endpoint(self, monkeypatch):
        # And not Together's key: sending a paid credential to an endpoint on
        # the LAN hands it to something that never needed it. This was
        # `services/z-image` until 2026-09-08; the path stays because
        # `IMAGE_API_URL` is still how a household points a role anywhere else.
        monkeypatch.setenv("IMAGE_API_MODEL", "a-self-hosted-model")
        monkeypatch.setenv("IMAGE_API_URL", "http://127.0.0.1:8083/v1/images/generations")
        model, url, key = fi._draw_endpoint("normal")
        assert model == "a-self-hosted-model"
        assert url == "http://127.0.0.1:8083/v1/images/generations"
        assert key == ""


class TestItRefusesClearly:
    """The model's only feedback channel is this JSON, so it must say why."""

    def test_no_prompt(self):
        assert "prompt" in fi.draw({"action": "draw"})["error"]

    def test_no_key(self, monkeypatch):
        monkeypatch.setattr(fi, "TOGETHER_KEY", "")
        err = fi.draw({"prompt": "globos"})["error"]
        assert "TOGETHER_API_KEY" in err and "search" in err

    def test_no_model_configured(self, monkeypatch):
        monkeypatch.delenv("TOGETHER_IMAGE_MODEL")
        monkeypatch.delenv("TOGETHER_IMAGE_MODEL_HIGH")
        assert "admin" in fi.draw({"prompt": "globos"})["error"]


class TestWhichModelPays:
    """The default decides what a drawing costs, so it is pinned here.

    `draw()` defaulted to `high` for as long as both slots were a paid API and
    the only question was which one. The house draws its own pictures now, and
    leaving that default would mean every incidental picture -- a thumbnail, a
    sketch nobody asked to be good -- billed a hosted provider.
    """

    def test_no_quality_means_the_everyday_slot(self, monkeypatch):
        monkeypatch.setenv("IMAGE_API_MODEL", "local-model")
        monkeypatch.setenv("IMAGE_API_URL", "http://127.0.0.1:8083/v1/images/generations")
        monkeypatch.setenv("IMAGE_API_MODEL_HIGH", "paid-model")
        monkeypatch.delenv("IMAGE_API_URL_HIGH", raising=False)
        model, url, key = fi._draw_endpoint("")
        assert model == "local-model", model
        assert url == "http://127.0.0.1:8083/v1/images/generations", url
        assert key == "", "a self-hosted endpoint must not be sent a paid key"

    def test_asking_for_high_still_reaches_the_paid_one(self, monkeypatch):
        monkeypatch.setenv("IMAGE_API_MODEL", "local-model")
        monkeypatch.setenv("IMAGE_API_URL", "http://127.0.0.1:8083/v1/images/generations")
        monkeypatch.setenv("IMAGE_API_MODEL_HIGH", "paid-model")
        monkeypatch.delenv("IMAGE_API_URL_HIGH", raising=False)
        model, url, key = fi._draw_endpoint("high")
        assert model == "paid-model", model
        assert url == fi.TOGETHER_URL, url
        # The key travels with the endpoint, and the hosted one needs it.
        assert key == fi.TOGETHER_KEY, key

    def test_paid_comes_off_the_credential_not_the_address(self, monkeypatch):
        """SKILL.md makes Alfred say who drew it, and it reads `paid` to do so.

        `url != TOGETHER_URL` looks like the same question and is not:
        `IMAGE_API_URL` with an `IMAGE_API_KEY` is the third case the manifest
        declares that key for -- an endpoint that is neither Together nor the
        house -- and announcing that one as "el modelo de la casa" is a false
        statement about a bill and about where the prompt went.
        """
        import base64 as _b64

        class _R:
            def read(self):
                return json.dumps({"data": [
                    {"b64_json": _b64.b64encode(b"PNG").decode()}]}).encode()

            def __enter__(self): return self

            def __exit__(self, *a): return False

        monkeypatch.setattr(fi.urllib.request, "urlopen",
                            lambda req, timeout=None: _R())
        monkeypatch.setenv("IMAGE_API_MODEL", "local-model")
        monkeypatch.setenv("IMAGE_API_URL", "http://127.0.0.1:8083/v1/images/generations")

        out = fi.draw({"prompt": "globos", "filename": "a.png"})
        assert (out["local"], out["paid"]) == (True, False), out

        # Same address, now wanting a bearer: somebody's paid API.
        monkeypatch.setenv("IMAGE_API_KEY", "a-paid-key")
        out = fi.draw({"prompt": "globos", "filename": "b.png"})
        assert (out["local"], out["paid"]) == (False, True), out


class TestTheDownload:
    """`_save_drawn`, which is where a drawing can go wrong quietly."""

    def _serve(self, monkeypatch, ctype="image/png", body=b"\x89PNG-data",
               final_url=None):
        # `cdn.together.ai` is a stand-in and does not resolve here, so the real
        # guard would reject it for the wrong reason. Everything else still goes
        # through the real one, which is what keeps the two checks below honest.
        real = fi.validate_url_target
        monkeypatch.setattr(fi, "validate_url_target", lambda u: (
            (True, "") if "cdn.together.ai" in u else real(u)))
        class _R:
            headers = {"Content-Type": ctype}
            def geturl(self): return final_url or "https://cdn.together.ai/x.png"
            def read(self, n=None): return body
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(fi.urllib.request, "urlopen", lambda *a, **k: _R())

    def test_a_together_url_is_accepted_though_it_is_not_in_the_catalogue_allowlist(
            self, monkeypatch):
        """The whole reason this does not go through `get`."""
        assert not fi._host_allowed("https://cdn.together.ai/x.png")
        self._serve(monkeypatch)
        out = fi._save_drawn("https://cdn.together.ai/x.png", "globos.png", "m")
        assert out["file"] == "media/globos.png"
        assert out["model"] == "m"

    def test_a_private_address_is_refused(self):
        out = fi._save_drawn("http://192.168.1.11:5001/snapshot", "x.png", "m")
        assert "error" in out and "unusable" in out["error"]

    def test_and_so_is_a_redirect_into_the_house(self, monkeypatch):
        self._serve(monkeypatch, final_url="http://127.0.0.1:8080/secret.png")
        out = fi._save_drawn("https://cdn.together.ai/x.png", "x.png", "m")
        assert "error" in out and "redirected" in out["error"]

    def test_something_that_is_not_an_image_is_refused(self, monkeypatch):
        self._serve(monkeypatch, ctype="text/html")
        assert "error" in fi._save_drawn("https://cdn.together.ai/x.png", "x.png", "m")

    def test_an_oversized_picture_is_refused(self, monkeypatch):
        self._serve(monkeypatch, body=b"x" * (fi.MAX_BYTES + 1))
        out = fi._save_drawn("https://cdn.together.ai/x.png", "x.png", "m")
        assert "MB" in out["error"]

    def test_the_name_cannot_walk_out_of_media(self, monkeypatch):
        self._serve(monkeypatch)
        out = fi._save_drawn("https://cdn.together.ai/x.png", "../../etc/passwd", "m")
        # basename strips the walk; the extension comes from the content type,
        # since a drawn file arrives named after nothing in particular.
        assert out["file"] == "media/passwd.png"


class TestTheRequest:
    def _capture(self, monkeypatch, reply):
        seen = {}

        class _R:
            def read(self): return json.dumps(reply).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            seen["body"] = json.loads(req.data.decode())
            seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
            return _R()

        monkeypatch.setattr(fi.urllib.request, "urlopen", fake_urlopen)
        # Same arity as the real one, `fell_back` included: a stub that is one
        # argument short of its subject turns a signature change into six
        # failures that say nothing about what broke.
        monkeypatch.setattr(
            fi, "_save_drawn",
            lambda url, name, model, fell_back=False, local=False: {
                "file": "media/x.png", "fell_back": fell_back, "local": local})
        return seen

    def test_it_sends_a_user_agent_because_cloudflare_bans_urllibs(self, monkeypatch):
        seen = self._capture(monkeypatch, {"data": [{"url": "https://cdn.together.ai/x.png"}]})
        fi.draw({"prompt": "globos"})
        assert seen["headers"].get("User-agent".lower()) == fi.DRAW_UA

    def test_shape_picks_a_size(self, monkeypatch):
        seen = self._capture(monkeypatch, {"data": [{"url": "https://cdn.together.ai/x.png"}]})
        fi.draw({"prompt": "globos", "shape": "portrait"})
        assert (seen["body"]["width"], seen["body"]["height"]) == (896, 1152)

    def test_an_unknown_shape_falls_back_to_the_size_every_model_takes(self, monkeypatch):
        seen = self._capture(monkeypatch, {"data": [{"url": "https://cdn.together.ai/x.png"}]})
        fi.draw({"prompt": "globos", "shape": "banner"})
        assert (seen["body"]["width"], seen["body"]["height"]) == (1024, 1024)

    def test_base64_is_accepted_when_there_is_no_url(self, monkeypatch, tmp_path):
        import base64
        self._capture(monkeypatch, {"data": [{"b64_json": base64.b64encode(b"PNG").decode()}]})
        out = fi.draw({"prompt": "globos", "filename": "g.png"})
        assert out["file"] == "media/g.png"
        assert (tmp_path / "media" / "g.png").read_bytes() == b"PNG"


class TestItIsOfferedToTheModel:
    def test_draw_is_a_real_action(self):
        assert fi.ACTIONS["draw"] is fi.draw

    def test_search_and_get_still_are(self):
        assert set(fi.ACTIONS) == {"search", "get", "draw"}
