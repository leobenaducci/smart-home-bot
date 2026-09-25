"""The reply promised a file and didn't attach it — deliver it anyway."""

import pytest

from nanobot.agent.loop import _MAX_RESCUED_LINKS, _undelivered_download_links

LINK = "[Front](download:media/cam_front_1785208081.jpg)"
OTHER = "[Patio](download:media/cam_patio_1785207655.jpg)"


def _tool(content):
    return {"role": "tool", "name": "exec", "content": content}


def test_rescues_a_link_the_reply_left_out():
    """The observed failure: snapshot ran, the file was written and served, and
    the model wrote 'aquí tiene 👇' with nothing after the arrow."""
    messages = [_tool('{"message": "Front, ahora mismo:\\n' + LINK + '"}')]

    links = _undelivered_download_links(messages, "Aquí tiene, señor Alex 👇")

    assert links == [LINK]


def test_stays_out_of_the_way_when_the_reply_has_the_link():
    """The normal case. Delivering here too would double every photo."""
    messages = [_tool('{"download_link": "' + LINK + '"}')]

    assert _undelivered_download_links(messages, f"Así está el frente:\n{LINK}") == []


def test_a_different_link_in_the_reply_still_counts_as_delivered():
    """The model showing *a* file is it choosing what to deliver; second-guessing
    which one would append photos to a reply that already has one."""
    messages = [_tool('{"download_link": "' + LINK + '"}')]

    assert _undelivered_download_links(messages, f"Mira: {OTHER}") == []


def test_deduplicates_repeated_links():
    messages = [_tool(LINK), _tool(LINK)]

    assert _undelivered_download_links(messages, "listo") == [LINK]


def test_keeps_distinct_files_in_order():
    messages = [_tool(OTHER), _tool(LINK)]

    assert _undelivered_download_links(messages, "listo") == [OTHER, LINK]


def test_ignores_links_outside_tool_results():
    """Only a tool actually produced a file. A link the user typed, or one the
    model invented in an earlier message, refers to nothing on disk."""
    messages = [
        {"role": "user", "content": f"mandame {LINK}"},
        {"role": "assistant", "content": f"como este {LINK}"},
    ]

    assert _undelivered_download_links(messages, "listo") == []


def test_tolerates_block_style_tool_content():
    messages = [_tool([{"type": "text", "text": LINK}]), _tool(LINK)]

    assert _undelivered_download_links(messages, "listo") == [LINK]


def test_caps_a_runaway_turn_and_keeps_the_newest():
    """A skill called in a loop should not paste a wall of photos; the last ones
    are the ones the user is waiting on."""
    made = [f"[c{i}](download:media/cam_{i}.jpg)" for i in range(7)]
    messages = [_tool(link) for link in made]

    links = _undelivered_download_links(messages, "listo")

    assert links == made[-_MAX_RESCUED_LINKS:]


def test_no_tool_output_means_nothing_to_deliver():
    assert _undelivered_download_links([], "hola") == []
    assert _undelivered_download_links([_tool("sin archivos")], None) == []


# --- falling back to the file itself -------------------------------------
# Observed in production: the model improvised its own snapshot script after the
# skill's exec died on a quoting bug, and printed only the path it had written.


def test_rescues_a_bare_media_path_when_no_link_was_produced():
    messages = [_tool("media/cam_front_1785210900.jpg\n")]

    links = _undelivered_download_links(messages, "Aquí tiene la cámara del frente 🏠")

    assert links == ["[cam_front_1785210900](download:media/cam_front_1785210900.jpg)"]


def test_prefers_a_real_link_over_the_path_fallback():
    """Both appear in the same result; the skill's own label should win rather
    than being joined by a second, uglier copy of the same photo."""
    messages = [_tool(
        '{"download_link": "' + LINK + '", "path": "media/cam_front_1785208081.jpg"}'
    )]

    assert _undelivered_download_links(messages, "listo") == [LINK]


def test_listing_tools_are_not_treated_as_deliveries():
    """glob/list_dir over the media dir enumerate files; none of them is a photo
    the reply forgot to attach."""
    listing = {
        "role": "tool",
        "name": "glob",
        "content": "media/cam_front_1.jpg\nmedia/cam_patio_2.jpg\nmedia/doc_3_thumb.jpg",
    }

    assert _undelivered_download_links([listing], "encontré tres") == []


def test_a_generated_document_is_rescued_like_a_photo():
    """This used to assert the opposite — that a document was "not something to
    paste inline unasked". The case that overturned it was not unasked: "mándame
    el PDF de la póliza" produced a real file in media/ and a reply saying "le
    envié el archivo" with nothing attached, because the rescue only matched
    images.

    The guards that keep this from pasting things nobody wanted are elsewhere
    and still hold: a reply that already carries a link is left alone, and
    listing tools are skipped."""
    messages = [_tool("media/informe.pdf")]

    assert _undelivered_download_links(messages, "listo") == [
        "[informe](download:media/informe.pdf)"
    ]


# --- history must not be rescued ----------------------------------------
#
# Observed after build 188: "muéstrame la cámara del patio" delivered the right
# patio photo AND a Front snapshot captured hours earlier. The Front link was
# still sitting in the session history, where this scan could see it and judge
# it undelivered. What is undelivered has to mean "in this turn".


def _user(content):
    return {"role": "user", "content": content}


def _assistant(content):
    return {"role": "assistant", "content": content}


def test_ignores_a_link_from_an_earlier_turn():
    messages = [
        {"role": "system", "content": "..."},
        _user("muéstrame el frente"),           # this morning
        _tool('{"message": "Front:\n' + LINK + '"}'),
        _assistant(f"Ahí tiene: {LINK}"),
        _user("muéstrame el patio"),            # ← this turn starts here
        _tool('{"message": "Patio:\n' + OTHER + '"}'),
    ]
    # 1 system + 3 history messages
    links = _undelivered_download_links(messages, "Ahí tiene el patio.", turn_start=4)

    assert links == [OTHER], "a Front photo from hours ago is not undelivered now"


def test_history_delivery_does_not_suppress_this_turns_rescue():
    """The same bug in the other direction: an old assistant message carrying a
    link would read as 'the file arrived' and cancel a rescue this turn needs."""
    messages = [
        {"role": "system", "content": "..."},
        _assistant(f"Ahí tiene: {LINK}"),       # a previous turn delivered one
        _user("ahora el patio"),
        _tool('{"message": "Patio:\n' + OTHER + '"}'),
    ]
    links = _undelivered_download_links(messages, "listo", turn_start=2)

    assert links == [OTHER]


def test_turn_start_defaults_to_scanning_everything():
    """Callers that have no history boundary keep the old behaviour."""
    messages = [_tool('{"message": "Front:\n' + LINK + '"}')]
    assert _undelivered_download_links(messages, "listo") == [LINK]


# --- documents, not only photos -----------------------------------------
#
# Asked for the insurance policy, the model bypassed the skill, wrote its own
# Python, saved a real PDF into media/ and said "le envié el archivo" having
# sent nothing. The rescue skipped it because a .pdf is not a .jpg — but the
# family cannot tell an undelivered document from an undelivered photo.


def test_rescues_a_pdf_the_reply_left_out():
    messages = [_tool(
        "Title: Poliza CHUBB\n"
        "Saved: /home/nanobot/.nanobot/workspace/media/doc_41_poliza_chubb.pdf (169452 bytes)\n"
        "Rel path: media/doc_41_poliza_chubb.pdf"
    )]

    links = _undelivered_download_links(messages, "Listo, le envié el archivo.")

    assert links == ["[doc_41_poliza_chubb](download:media/doc_41_poliza_chubb.pdf)"]


@pytest.mark.parametrize("name", [
    "informe.docx", "gastos.xlsx", "minuta.txt", "grabacion.m4a", "video.mp4",
])
def test_rescues_the_other_things_a_family_gets_sent(name):
    links = _undelivered_download_links([_tool(f"wrote media/{name}")], "ahí va")
    assert links and name in links[0]


def test_a_listing_that_mentions_a_pdf_is_not_an_undelivered_file():
    """Widening the pattern must not turn `ls` into a delivery."""
    listing = {"role": "tool", "name": "list_dir",
               "content": "media/doc_41.pdf\nmedia/doc_42.pdf"}
    assert _undelivered_download_links([listing], "encontré dos") == []
