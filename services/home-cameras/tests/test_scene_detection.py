"""The assistant asks the cameras what they can see, and gets data not prose.

Background: "¿hay alguien en el patio?" was answered by sending the JPEG to a
vision-language model. On real night frames from these cameras the small VLMs
invented "una persona con chaqueta oscura" in an empty patio on half their
runs, and the large one needed 10-40s to reach the answer a detector reaches in
milliseconds with a confidence score attached.

``cv2``, ``torch`` and ``ultralytics`` are only installed in the deployed image,
so importing web_server or object_detector here fails on a dev machine. These
read the source instead and exercise the parts that are pure Python: the frame
geometry handed to the assistant, and the flag separating "worth an alert" from
"worth describing".
"""
import ast
from pathlib import Path

import pytest

WEB_SERVER = Path(__file__).resolve().parents[1] / "web_server"


def _load_function(filename: str, name: str):
    """Compile one top-level function out of a module we cannot import."""
    tree = ast.parse((WEB_SERVER / filename).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            module = ast.Module(body=[node], type_ignores=[])
            namespace: dict = {}
            exec(compile(module, filename, "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"{name} not found in {filename}")


@pytest.fixture(scope="module")
def frame_position():
    return _load_function("web_server.py", "_frame_position")


# --- frame geometry ------------------------------------------------------


@pytest.mark.parametrize("cx,cy,expected", [
    (50, 50, "arriba-izquierda"),
    (500, 50, "arriba-centro"),
    (950, 50, "arriba-derecha"),
    (50, 500, "medio-izquierda"),
    (500, 500, "medio-centro"),
    (950, 950, "abajo-derecha"),
])
def test_position_is_named_in_the_language_it_will_be_spoken_in(
    frame_position, cx, cy, expected
):
    """These words go straight into a Spanish sentence, so they are Spanish."""
    assert frame_position(cx, cy, 1000, 1000) == expected


def test_position_scales_with_the_frame(frame_position):
    """The patio and front cameras are mounted rotated and deliver 1296x2304.
    A fixed pixel threshold would read every one of those frames as top-left."""
    assert frame_position(100, 1800, 1296, 2304) == "abajo-izquierda"
    assert frame_position(648, 1150, 1296, 2304) == "medio-centro"


# --- which classes get reported -----------------------------------------


def _init_defaults(filename: str, classname: str) -> dict:
    """Default values of a class's __init__ keyword arguments, read statically."""
    tree = ast.parse((WEB_SERVER / filename).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == classname:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    args = item.args.args[1:]           # drop self
                    defaults = item.args.defaults
                    named = args[len(args) - len(defaults):]
                    return {
                        a.arg: ast.literal_eval(d)
                        for a, d in zip(named, defaults)
                    }
    raise AssertionError(f"{classname}.__init__ not found")


def test_describing_everything_is_opt_in():
    """The per-camera motion detectors run continuously on every frame of every
    camera and must keep their narrow alert filter. Widening that by default
    would turn every passing chair into something worth waking someone for."""
    defaults = _init_defaults("object_detector.py", "ObjectDetector")
    assert defaults["detect_all_classes"] is False


def test_the_scene_endpoint_asks_for_every_class():
    """...and the on-demand endpoint opts in, because "¿qué hay en el patio?"
    wants the chairs and the potted plant, not only people and cars."""
    source = (WEB_SERVER / "web_server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "ObjectDetector"):
            kwargs = {k.arg: k.value for k in node.keywords}
            assert "detect_all_classes" in kwargs
            assert ast.literal_eval(kwargs["detect_all_classes"]) is True
            return
    raise AssertionError("no ObjectDetector construction found in web_server.py")


def test_scene_model_is_the_largest_weights_available():
    """Asked for explicitly: the on-demand path is not latency-critical, so it
    uses the biggest model rather than the one motion detection settles for."""
    source = (WEB_SERVER / "web_server.py").read_text(encoding="utf-8")
    assert "yolo26x.pt" in source
    models = sorted((WEB_SERVER / "models").glob("yolo26*.pt"))
    if models:  # weights are not committed on every checkout
        largest = max(models, key=lambda p: p.stat().st_size)
        assert largest.name == "yolo26x.pt", (
            f"{largest.name} is larger than the model the endpoint requests"
        )


# --- confidence handling -------------------------------------------------
#
# Measured on an empty night patio, yolo26x reported `person` at 0.35 and two
# `bed` at 0.35-0.38 (the pergola). YOLO is not immune to seeing things in the
# dark; what it has that a vision-language model does not is a number saying so.
# The assistant is fluent, so a bare 0.35 becomes a confident sentence unless
# something tells it otherwise.


@pytest.fixture(scope="module")
def certainty():
    return _load_function("web_server.py", "_certainty")


@pytest.mark.parametrize("confidence,expected", [
    (0.95, "alta"),      # the chair in the living room
    (0.85, "alta"),      # the car in the driveway
    (0.70, "alta"),
    (0.53, "media"),
    (0.45, "media"),
    (0.38, "baja"),      # the "bed" that is a pergola
    (0.35, "baja"),      # the "person" in an empty patio
])
def test_certainty_bands(certainty, confidence, expected):
    assert certainty(confidence) == expected


def test_the_observed_false_positive_lands_in_the_lowest_band(certainty):
    """The specific failure this guards: a person who is not there."""
    assert certainty(0.35) == "baja"


def test_motion_thresholds_are_not_reused_for_questions():
    """object_detector floors person/cat/dog at 0.20 for alert sensitivity —
    right for waking someone, wrong for answering. The scene detector clears
    those overrides instead of inheriting them."""
    source = (WEB_SERVER / "web_server.py").read_text(encoding="utf-8")
    assert "class_confidence_thresholds = {}" in source
    assert "SCENE_MIN_CONFIDENCE" in source


def test_payload_tells_the_assistant_how_to_treat_low_certainty():
    """The note travels with the data; a caller that ignores the number should
    still be told in words."""
    source = (WEB_SERVER / "web_server.py").read_text(encoding="utf-8")
    assert 'certainty="baja"' in source and "never" in source
