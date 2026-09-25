"""What the undelivered-file rescue picks up, now that links point at the share.

The rescue exists because models announce an attachment and then omit it. Moving
durable files onto the family share changed the shape of every link it reads —
they carry folders now, and names people typed — and widened what a tool result
means: saving, copying and moving all hand back a ready-made link so the model
never writes a path from memory. Each of those is a way for this to send the
wrong thing, or nothing.
"""
import re

from nanobot.agent.loop import (
    _DOWNLOAD_LINK_RE,
    _MEDIA_IMAGE_RE,
    _NOT_A_DELIVERY_RE,
    _link_target,
    _newest_per_subject,
    _undelivered_download_links,
)


def _turn(tool_results, reply):
    """One turn: some tool output, then the model's reply."""
    messages = [{"role": "user", "content": "…"}]
    for content in tool_results:
        messages.append({"role": "tool", "name": "exec", "content": content})
    return _undelivered_download_links(messages, reply, turn_start=1)


# --- the link shapes themselves ------------------------------------------


def test_a_filename_with_parentheses_is_still_one_link():
    # Bare markdown ends the URL at the first ")", so this is emitted in the
    # angle form. Read it back or the rescue believes nothing was delivered and
    # sends the file again.
    link = "[Factura (1).pdf](<download:user1/alfred/Factura (1).pdf>)"
    assert _DOWNLOAD_LINK_RE.findall(link) == [link]
    assert _link_target(link) == "user1/alfred/Factura (1).pdf"


def test_the_plain_form_still_reads():
    link = "[informe.md](download:user1/alfred/informe.md)"
    assert _DOWNLOAD_LINK_RE.findall(link) == [link]
    assert _link_target(link) == "user1/alfred/informe.md"


def test_a_name_with_spaces_counts_as_delivered():
    # `[^)\s]+` used to end the target at the first space, so a delivered
    # picture read as no link at all and was sent a second time.
    reply = "Aquí está: [Lámina 1 — Portada.png](download:user1/alfred/Lámina 1 — Portada.png)"
    assert _turn(["saved"], reply) == []


# --- deliveries versus side effects ---------------------------------------


def test_a_save_is_not_re_broadcast():
    # "ordena mi carpeta, mueve los 6 PDF" is not six announcements. The reply
    # correctly carries no link; posting one per moved file is noise.
    moves = [
        '{"ok": true, "dst": "user1/alfred/viejos/a%d.pdf", '
        '"download_link": "[a%d.pdf](download:user1/alfred/viejos/a%d.pdf)", '
        '"deliver": false}' % (i, i, i)
        for i in range(6)
    ]
    assert _turn(moves, "Listo, quedaron en alfred/viejos/.") == []


def test_but_a_real_delivery_still_gets_rescued():
    handed_over = ('{"ok": true, "remote_path": "user2/recetas/pastel.md", '
                   '"download_link": "[pastel.md](download:user2/recetas/pastel.md)"}')
    assert _turn([handed_over], "Te lo mando 👇") == [
        "[pastel.md](download:user2/recetas/pastel.md)"
    ]


def test_the_sentinel_is_read_however_it_was_serialised():
    for spelling in ('"deliver": false', '"deliver":false', '"deliver": False'):
        assert _NOT_A_DELIVERY_RE.search("{%s}" % spelling), spelling
    assert not _NOT_A_DELIVERY_RE.search('{"deliver": true}')


# --- one subject, one file -------------------------------------------------


def test_two_files_of_the_same_name_in_different_folders_both_arrive():
    # Grouping on the bare stem was safe while every rescued file sat in one
    # flat media/ dir. On the share these are two different files.
    links = [
        "[lista.md](download:user1/alfred/lista.md)",
        "[lista.md](download:familia/lista.md)",
    ]
    assert sorted(_newest_per_subject(links)) == sorted(links)


def test_the_same_camera_twice_is_still_one_photo():
    links = [
        "[cam_front.jpg](download:media/cam_front_1785263359.jpg)",
        "[cam_front.jpg](download:media/cam_front_1785263400.jpg)",
    ]
    assert _newest_per_subject(links) == [links[-1]]


def test_two_different_cameras_are_two_photos():
    links = [
        "[cam_front.jpg](download:media/cam_front_1785263359.jpg)",
        "[cam_patio.jpg](download:media/cam_patio_1785263360.jpg)",
    ]
    assert len(_newest_per_subject(links)) == 2


# --- the fallback signal ---------------------------------------------------


def test_a_bare_media_path_named_in_spanish_is_found():
    # The class was `[\w.\-]+`, so a space made the file invisible here and the
    # picture the model improvised was never rescued.
    assert _MEDIA_IMAGE_RE.findall("guardé media/Lámina 1 — Portada.png") == [
        "media/Lámina 1 — Portada.png"
    ]


def test_prose_is_not_swallowed_into_the_filename():
    # Spaces are allowed one at a time and never immediately before the
    # extension, so a match stops at the file instead of running on through the
    # sentence to a later ".png". Here the path is real and only the path is
    # taken — `media/a.txt` and not `media/a.txt es un .png`.
    assert _MEDIA_IMAGE_RE.findall("guardé media/a.txt es un .png ahí") == ["media/a.txt"]
    # And where there is no filename at all, nothing is invented.
    assert _MEDIA_IMAGE_RE.findall("mira media/ el archivo .png") == []


def test_two_paths_in_one_line_stay_two():
    assert _MEDIA_IMAGE_RE.findall("ver media/x.png y media/y.png") == [
        "media/x.png", "media/y.png",
    ]


def test_the_fallback_only_runs_when_no_link_was_offered():
    # A tool that handed back a proper link is not improvising, so the bare-path
    # signal must not add a second copy of the same file.
    result = _turn(
        ['{"download_link": "[x.png](download:media/x.png)"}', "wrote media/x.png"],
        "Te la mando 👇",
    )
    assert result == ["[x.png](download:media/x.png)"]


def test_a_directory_listing_is_not_an_undelivered_photo():
    messages = [
        {"role": "user", "content": "…"},
        {"role": "tool", "name": "list_dir", "content": "media/foto.png\nmedia/otra.png"},
    ]
    assert _undelivered_download_links(messages, "Hay dos fotos.", turn_start=1) == []
