"""The listening bench: /v1/tts and /v1/tts/voices.

These exist so a voice can be chosen by ear rather than by argument, and the
things worth pinning are the ones that make the dropdown honest:

- a voice needs both its .onnx and its .onnx.json, or it is not offered;
- an engine that is configured but down looks different from one that was
  never set up, because the first is worth fixing and the second is not;
- naming a voice that does not exist says which ones do;
- and the house default never changes just because somebody was comparing.

    docker run --rm -v "$PWD/test:/test:ro" voice-gateway:latest \\
        pytest -q /test/test_tts_bench.py
"""
import pytest
from fastapi.testclient import TestClient

from conftest import H  # noqa: F401  — the stubs and sys.path live there


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import gateway

    baked = tmp_path / "voice.onnx"
    baked.write_bytes(b"onnx")
    (tmp_path / "voice.onnx.json").write_text('{"audio":{"sample_rate":22050}}')

    voices = tmp_path / "voices"
    voices.mkdir()
    for name in ("es_MX-ald-medium", "es_ES-carlfm-x_low"):
        (voices / f"{name}.onnx").write_bytes(b"onnx")
        (voices / f"{name}.onnx.json").write_text('{"audio":{"sample_rate":16000}}')
    # A voice missing its config: half a voice is not a voice.
    (voices / "roto.onnx").write_bytes(b"onnx")

    monkeypatch.setattr(gateway, "PIPER_MODEL", str(baked))
    monkeypatch.setattr(gateway, "PIPER_VOICES_DIR", str(voices))
    monkeypatch.setattr(gateway, "GATEWAY_TOKEN", "tok")
    monkeypatch.setattr(gateway, "QWEN_TTS_URL", "")
    monkeypatch.setattr(gateway, "DEFAULT_TTS_ENGINE", "piper")

    spoken = []

    async def fake_piper(text, voice=None):
        spoken.append(("piper", voice, text))
        return b"\x00\x01" * 2205, 22050        # 0.1 s

    async def fake_qwen(text, voice=None):
        spoken.append(("qwen", voice, text))
        return b"\x00\x01" * 2400, 24000

    # Kept before patching so a test can put one real function back without
    # monkeypatch.undo() also reverting the voice directory underneath it.
    real_piper, real_qwen = gateway._synthesize_piper, gateway._synthesize_qwen
    monkeypatch.setattr(gateway, "_synthesize_piper", fake_piper)
    monkeypatch.setattr(gateway, "_synthesize_qwen", fake_qwen)

    c = TestClient(gateway.app)
    c.gateway = gateway
    c.spoken = spoken
    c.real_piper, c.real_qwen = real_piper, real_qwen
    return c


# --- what the dropdown is built from ----------------------------------------

def test_the_roster_needs_the_gateway_token(client):
    assert client.get("/v1/tts/voices").status_code == 401
    assert client.post("/v1/tts", json={"text": "hola"}).status_code == 401


def test_the_baked_voice_is_always_there_and_is_the_default(client):
    r = client.get("/v1/tts/voices", headers=H).json()
    piper = [e for e in r["engines"] if e["id"] == "piper"][0]
    ids = [v["id"] for v in piper["voices"]]
    assert "default" in ids
    assert piper["available"] is True
    assert piper["default_voice"] == "default"
    assert r["default_engine"] == "piper"


def test_dropped_in_voices_are_offered(client):
    piper = [e for e in client.get("/v1/tts/voices", headers=H).json()["engines"]
             if e["id"] == "piper"][0]
    ids = [v["id"] for v in piper["voices"]]
    assert "es_MX-ald-medium" in ids and "es_ES-carlfm-x_low" in ids


def test_a_voice_without_its_config_is_not_offered(client):
    """Half a voice fails at synthesis time, which is after somebody picked it."""
    piper = [e for e in client.get("/v1/tts/voices", headers=H).json()["engines"]
             if e["id"] == "piper"][0]
    assert "roto" not in [v["id"] for v in piper["voices"]]


def test_an_unconfigured_engine_says_so_rather_than_vanishing(client):
    qwen = [e for e in client.get("/v1/tts/voices", headers=H).json()["engines"]
            if e["id"] == "qwen"][0]
    assert qwen["available"] is False
    assert "not configured" in qwen["detail"]


def test_a_configured_but_dead_engine_reads_differently(client, monkeypatch):
    """The two are worth different things: one is a bug, one is a decision."""
    monkeypatch.setattr(client.gateway, "QWEN_TTS_URL", "http://127.0.0.1:9")  # nothing listens
    qwen = [e for e in client.get("/v1/tts/voices", headers=H).json()["engines"]
            if e["id"] == "qwen"][0]
    assert qwen["available"] is False
    assert "not responding" in qwen["detail"]


def test_the_roster_still_answers_when_the_sidecar_is_down(client, monkeypatch):
    """Piper working has to survive Qwen being broken — otherwise a bench that
    is half up takes the whole menu with it."""
    monkeypatch.setattr(client.gateway, "QWEN_TTS_URL", "http://127.0.0.1:9")
    r = client.get("/v1/tts/voices", headers=H)
    assert r.status_code == 200
    assert [e for e in r.json()["engines"] if e["id"] == "piper"][0]["available"] is True


# --- speaking ---------------------------------------------------------------

def test_it_comes_back_as_a_playable_wav(client):
    r = client.post("/v1/tts", headers=H, json={"text": "Buenas tardes."})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    # A browser needs the container; the puck's own path still gets raw PCM.
    assert r.content[:4] == b"RIFF" and r.content[8:12] == b"WAVE"


def test_the_timings_come_back_in_headers(client):
    """Which engine is fast enough is half the comparison, and asking twice to
    find out would change the answer."""
    r = client.post("/v1/tts", headers=H, json={"text": "hola"})
    assert r.headers["X-Engine"] == "piper"
    assert float(r.headers["X-Audio-Seconds"]) == pytest.approx(0.1, abs=0.01)
    assert float(r.headers["X-Synth-Seconds"]) >= 0


def test_naming_an_engine_routes_to_it(client):
    client.post("/v1/tts", headers=H, json={"text": "hola", "engine": "qwen"})
    assert client.spoken[-1][0] == "qwen"
    client.post("/v1/tts", headers=H, json={"text": "hola", "engine": "piper"})
    assert client.spoken[-1][0] == "piper"


def test_naming_nothing_gets_the_house_default(client):
    client.post("/v1/tts", headers=H, json={"text": "hola"})
    assert client.spoken[-1][:2] == ("piper", None)


def test_comparing_voices_never_moves_the_house_default(client):
    """The bench is a bench. A room, an announcement and a reply keep using
    whatever DEFAULT_TTS_ENGINE says, however much somebody clicked."""
    before = client.gateway.DEFAULT_TTS_ENGINE
    client.post("/v1/tts", headers=H, json={"text": "hola", "engine": "qwen", "voice": "x"})
    assert client.gateway.DEFAULT_TTS_ENGINE == before


def test_empty_text_is_a_400(client):
    assert client.post("/v1/tts", headers=H, json={"text": "   "}).status_code == 400


def test_a_pasted_document_is_truncated_not_refused(client):
    """It is a mistake, not an attack, and half of it read aloud is a clearer
    signal than an error nobody expected."""
    client.post("/v1/tts", headers=H, json={"text": "a" * 5000})
    assert len(client.spoken[-1][2]) == client.gateway.TTS_MAX_CHARS


def test_asking_for_qwen_with_no_sidecar_is_a_503(client, monkeypatch):
    monkeypatch.setattr(client.gateway, "_synthesize_qwen", client.real_qwen)
    r = client.post("/v1/tts", headers=H, json={"text": "hola", "engine": "qwen"})
    assert r.status_code == 503


def test_an_unknown_piper_voice_names_the_real_ones(client, monkeypatch):
    monkeypatch.setattr(client.gateway, "_synthesize_piper", client.real_piper)
    r = client.post("/v1/tts", headers=H, json={"text": "hola", "voice": "no-existe"})
    assert r.status_code == 404
    assert "default" in r.json()["detail"]


# --- naming an engine that does not exist ------------------------------------

@pytest.mark.parametrize("engine", ["Qwen", "qwen3", "Qwen3-TTS", "kokoro", " qwen"])
def test_an_engine_nobody_offers_is_a_400_not_quietly_piper(client, engine):
    """The bench exists to compare two engines on the same sentence. Falling
    back to piper while stamping the requested name into X-Engine made it
    compare piper against piper and label one of them as the other."""
    r = client.post("/v1/tts", headers=H, json={"text": "hola", "engine": engine})
    assert r.status_code == 400, r.text
    assert "piper" in r.json()["detail"] and "qwen" in r.json()["detail"]
    assert not client.spoken, "nothing should have been synthesized"


def test_a_misspelled_house_default_is_refused_rather_than_silently_piper(client, monkeypatch):
    """DEFAULT_TTS_ENGINE is the documented way to promote a winner. Spelled
    wrong it used to leave every room, announcement and reply on piper while
    /v1/tts/voices cheerfully reported the name that was set."""
    monkeypatch.setattr(client.gateway, "DEFAULT_TTS_ENGINE", "Qwen")
    r = client.post("/v1/tts", headers=H, json={"text": "hola"})
    assert r.status_code == 400
    assert not client.spoken


def test_the_engine_header_is_the_engine_that_spoke(client):
    for engine in ("piper", "qwen"):
        r = client.post("/v1/tts", headers=H, json={"text": "hola", "engine": engine})
        assert r.headers["X-Engine"] == engine == client.spoken[-1][0]


# --- the house voice is not a name anyone gets to claim ----------------------

def test_a_dropped_in_default_onnx_does_not_become_the_house_voice(client, tmp_path):
    """`/config/voices` is documented as "copy a file in", so naming a candidate
    default.onnx is the obvious thing to try — and it used to repoint every
    room, announcement and reply with no restart and no log line."""
    voices = tmp_path / "voices"
    (voices / "default.onnx").write_bytes(b"onnx")
    (voices / "default.onnx.json").write_text('{"audio":{"sample_rate":16000}}')

    assert client.gateway._piper_voices()["default"] == client.gateway.PIPER_MODEL


def test_a_voices_dir_that_is_not_a_directory_does_not_mute_the_house(client, monkeypatch, tmp_path):
    """Only FileNotFoundError was caught, so a `voices` that is a plain file
    took every reply down with it while the baked voice sat there working."""
    notadir = tmp_path / "not-a-dir"
    notadir.write_text("oops")
    monkeypatch.setattr(client.gateway, "PIPER_VOICES_DIR", str(notadir))

    assert "default" in client.gateway._piper_voices()
    assert client.post("/v1/tts", headers=H, json={"text": "hola"}).status_code == 200


# --- audio.cpp, and what catches it when it falls over -----------------------
#
# The rule these pin is one line with three cases in it, and getting any of
# them wrong is silent:
#
#   a name nobody offers      -> 400, because it is a typo
#   an engine never set up    -> 503, because falling back would hide it
#   an engine that is down    -> piper, because the family should not hear
#                                silence while a container restarts
#
# and in the third case the response has to say piper spoke. Reporting the
# engine that was *asked for* is the bug this file already exists to prevent;
# a fallback that lied the same way would be the same bug with a longer story.

@pytest.fixture()
def audiocpp(client, monkeypatch):
    """A working audio.cpp, and a way to break it mid-test."""
    import gateway
    state = {"fail": None}

    async def fake_audiocpp(text, voice=None, model=None):
        client.spoken.append(("audiocpp", voice, text))
        state["model"] = model
        if state["fail"] is not None:
            raise state["fail"]
        return b"\x00\x01" * 1200, 24000

    monkeypatch.setattr(gateway, "_synthesize_audiocpp", fake_audiocpp)
    monkeypatch.setattr(gateway, "AUDIOCPP_TTS_URL", "http://audio-cpp:8080")
    monkeypatch.setattr(gateway, "TTS_FALLBACK_ENGINE", "piper")
    client.break_audiocpp = lambda exc: state.__setitem__("fail", exc)
    client.audiocpp_state = state
    return client


def test_audiocpp_speaks_and_says_it_did(audiocpp):
    r = audiocpp.post("/v1/tts", json={"text": "hola", "engine": "audiocpp"},
                      headers=H)
    assert r.status_code == 200
    assert r.headers["X-Engine"] == "audiocpp"
    assert r.headers["X-Fallback"] == "0"
    assert ("audiocpp", None, "hola") in audiocpp.spoken


def test_a_model_that_is_not_installed_is_a_404_not_piper(audiocpp):
    from fastapi import HTTPException
    audiocpp.break_audiocpp(HTTPException(status_code=404, detail="no model"))
    r = audiocpp.post("/v1/tts",
                      json={"text": "hola", "engine": "audiocpp", "voice": "nope"},
                      headers=H)
    # A mistake in the request. Answering it with the house voice would make
    # a typed model id sound like a working configuration.
    assert r.status_code == 404
    assert not any(e == "piper" for e, _, _ in audiocpp.spoken)


def test_an_engine_that_is_down_falls_back_to_piper(audiocpp):
    from fastapi import HTTPException
    audiocpp.break_audiocpp(HTTPException(status_code=502, detail="connection refused"))
    r = audiocpp.post("/v1/tts", json={"text": "hola", "engine": "audiocpp"},
                      headers=H)
    assert r.status_code == 200
    # What spoke, not what was asked for.
    assert r.headers["X-Engine"] == "piper"
    assert r.headers["X-Engine-Requested"] == "audiocpp"
    assert r.headers["X-Fallback"] == "1"
    assert ("piper", None, "hola") in audiocpp.spoken


def test_the_fallback_does_not_carry_the_failed_engines_voice(audiocpp):
    from fastapi import HTTPException
    audiocpp.break_audiocpp(HTTPException(status_code=503, detail="loading"))
    r = audiocpp.post("/v1/tts",
                      json={"text": "hola", "engine": "audiocpp",
                            "voice": "supertonic_3_q8_0"},
                      headers=H)
    assert r.status_code == 200
    # piper has no voice by that name; passing it through would turn a
    # recoverable outage into a 404 from the thing meant to rescue it.
    assert ("piper", None, "hola") in audiocpp.spoken


def test_an_engine_never_configured_does_not_fall_back(client, monkeypatch):
    # The real function, not the fixture's stub: what is being pinned is the
    # refusal it raises when QWEN_TTS_URL is empty. This is "there is no
    # sidecar", not "the sidecar is restarting", and piper answering it would
    # leave a household believing they had switched voices.
    import gateway
    monkeypatch.setattr(gateway, "_synthesize_qwen", client.real_qwen)
    monkeypatch.setattr(gateway, "QWEN_TTS_URL", "")
    r = client.post("/v1/tts", json={"text": "hola", "engine": "qwen"}, headers=H)
    assert r.status_code == 503
    assert not any(e == "piper" for e, _, _ in client.spoken)


def test_turning_the_fallback_off_lets_the_failure_through(audiocpp, monkeypatch):
    import gateway
    from fastapi import HTTPException
    monkeypatch.setattr(gateway, "TTS_FALLBACK_ENGINE", "")
    audiocpp.break_audiocpp(HTTPException(status_code=502, detail="refused"))
    r = audiocpp.post("/v1/tts", json={"text": "hola", "engine": "audiocpp"},
                      headers=H)
    assert r.status_code == 502


def test_the_roster_names_the_fallback(audiocpp):
    r = audiocpp.get("/v1/tts/voices", headers=H).json()
    assert r["fallback_engine"] == "piper"
    ac = [e for e in r["engines"] if e["id"] == "audiocpp"][0]
    # Unreachable in this fixture -- the URL is set and nothing answers it --
    # which has to look different from never configured.
    assert ac["available"] is False
    assert "not responding" in ac.get("detail", "")


# --- the house voice, and why it does not travel -----------------------------

def test_the_default_voice_applies_to_the_default_engine(client, monkeypatch):
    import gateway
    monkeypatch.setattr(gateway, "DEFAULT_TTS_ENGINE", "piper")
    monkeypatch.setattr(gateway, "DEFAULT_TTS_VOICE", "es_MX-ald-medium")
    client.post("/v1/tts", json={"text": "hola"}, headers=H)
    assert ("piper", "es_MX-ald-medium", "hola") in client.spoken


def test_it_is_not_handed_to_an_engine_it_does_not_belong_to(audiocpp, monkeypatch):
    # `es_MX-ald-medium` is a piper voice. Passing it to audio.cpp because it
    # happens to be the house default would be a 404 caused by a setting the
    # caller never mentioned.
    import gateway
    monkeypatch.setattr(gateway, "DEFAULT_TTS_ENGINE", "piper")
    monkeypatch.setattr(gateway, "DEFAULT_TTS_VOICE", "es_MX-ald-medium")
    audiocpp.post("/v1/tts", json={"text": "hola", "engine": "audiocpp"}, headers=H)
    assert ("audiocpp", None, "hola") in audiocpp.spoken


def test_an_explicit_voice_still_wins(client, monkeypatch):
    import gateway
    monkeypatch.setattr(gateway, "DEFAULT_TTS_VOICE", "es_MX-ald-medium")
    client.post("/v1/tts", json={"text": "hola", "voice": "es_ES-carlfm-x_low"},
                headers=H)
    assert ("piper", "es_ES-carlfm-x_low", "hola") in client.spoken


def test_empty_means_the_engines_own_default(client, monkeypatch):
    import gateway
    monkeypatch.setattr(gateway, "DEFAULT_TTS_VOICE", "")
    client.post("/v1/tts", json={"text": "hola"}, headers=H)
    assert ("piper", None, "hola") in client.spoken


def test_the_roster_says_what_the_house_speaks_with(client, monkeypatch):
    import gateway
    monkeypatch.setattr(gateway, "DEFAULT_TTS_VOICE", "es_MX-claude-high")
    r = client.get("/v1/tts/voices", headers=H).json()
    assert r["default_voice"] == "es_MX-claude-high"


# --- a package and a voice inside it are two different things ----------------

def test_the_package_is_the_model_and_the_voice_is_a_style(audiocpp, monkeypatch):
    """The first version sent `voice` as the model, which made supertonic's ten
    styles unreachable: asking for M3 tried to load a package called M3.

    Easy to miss, because audio.cpp answers 200 to a voice it does not know and
    returns the default -- so a wrong field name looks exactly like a working
    one unless you compare the bytes, and comparing them with M1 compares the
    default against the default.
    """
    import gateway
    sent = {}

    async def capture(text, voice=None, model=None):
        sent["voice"] = voice
        sent["model"] = model
        return b"\x00\x01" * 1200, 24000

    monkeypatch.setattr(gateway, "_synthesize_audiocpp", capture)
    audiocpp.post("/v1/tts",
                  json={"text": "hola", "engine": "audiocpp", "voice": "M3"},
                  headers=H)
    # It reaches the engine as a voice, not as a model. What the engine then
    # puts in which JSON field is its own business and is asserted by the
    # smoke test against a real container.
    assert sent["voice"] == "M3"


def test_a_preview_can_name_a_package_without_committing_to_it(audiocpp):
    """The Listen button auditions a package the household has not chosen.

    Without this the preview could only ever test whatever `AUDIOCPP_TTS_MODEL`
    already is, which makes "listen before you choose" impossible for every
    package except the one already chosen.
    """
    audiocpp.post("/v1/tts",
                  json={"text": "hola", "engine": "audiocpp",
                        "model": "cosyvoice3_q8_0", "voice": "M3"},
                  headers=H)
    assert audiocpp.audiocpp_state["model"] == "cosyvoice3_q8_0"


def test_the_fallback_drops_the_package_too(audiocpp):
    from fastapi import HTTPException
    audiocpp.break_audiocpp(HTTPException(status_code=502, detail="down"))
    r = audiocpp.post("/v1/tts",
                      json={"text": "hola", "engine": "audiocpp",
                            "model": "cosyvoice3_q8_0"},
                      headers=H)
    # piper has no packages at all; carrying one down would turn an outage
    # into a 404 from the thing meant to rescue it.
    assert r.status_code == 200
    assert ("piper", None, "hola") in audiocpp.spoken
