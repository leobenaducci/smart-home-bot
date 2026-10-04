"""Eye contact: a recording's camera with the eyes moved to the lens.

    python -m studio.eyes <job.json>

A person recording a tutorial looks at the screen, a little below or beside
the camera, so the camera shows them looking away. This moves the pupils --
only the pupils: LivePortrait's expression keypoints for the eyes (11 and 15),
rendered on the face crop and pasted back over the eyes alone -- so that where
they look is the lens. The rest of the face is the recording's own pixels.

It is asked for, never automatic, and only over the stretches a person marks
(`spans`; none means the whole clip): looking away on purpose, at the
keyboard or at somebody in the room, is part of a recording. Each stretch
eases in and out so the eyes travel rather than jump.

What is corrected is the slow part of where the eyes point -- the gaze
smoothed over about half a second -- so the quick movements of reading stay
and a frame already looking at the lens is left alone (a dead band). Blinks
and eyes half shut are left as they are, and so are eyes opened much wider
than this person's usual: moving the pupils of either opens them into a
cartoon's (measured 2026-10-03; docs/home-studio.md).

Two passes over the file, streaming: a ten-minute camera track is 18,000
frames, about 50 GB held at once. The first measures the gaze; the second
renders. job.json: {"src", "dst", "spans": [[a, b], ...], "strength"}.
Progress goes to stdout, one JSON object per line: {"progress", "phase"};
a failure is {"error"} and a non-zero exit.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

LP_ROOT = Path(os.environ.get("LIVEPORTRAIT_ROOT", "/opt/liveportrait"))
WEIGHTS = Path(os.environ.get("LIVEPORTRAIT_WEIGHTS", "/opt/wangp/ckpts/liveportrait"))
WEIGHT_FILES = (
    "insightface/models/buffalo_l/2d106det.onnx", "insightface/models/buffalo_l/det_10g.onnx",
    "liveportrait/base_models/appearance_feature_extractor.pth", "liveportrait/base_models/motion_extractor.pth",
    "liveportrait/base_models/spade_generator.pth", "liveportrait/base_models/warping_module.pth",
    "liveportrait/landmark.onnx", "liveportrait/retargeting_models/stitching_retargeting_module.pth")

# Where the pupils sit, in eye widths from the corners' midpoint, when a face
# looks into the lens: the median of twelve clips of people facing the camera
# -- centred, and a little above the corners' line.
LOOK = np.array([0.0, -0.08])
DEAD = 0.012          # nearer the lens than this, in eye widths: nothing to correct
CAP = np.array([0.02, 0.025])   # the largest keypoint shift, sideways and up/down
EASE = 0.25           # seconds a stretch takes to come in and to go out
SMOOTH = 0.25         # the gaze's smoothing, a Gaussian's sigma in seconds
MIN_GAIN = 4.0        # less response than this to a probe: that direction is left alone
CROP_SCALE = 1.8      # the face fills more of the 512 px render than LivePortrait's 2.3
SHARPEN = 0.6         # an unsharp mask on the render, for the edge a 512 px crop loses
L_EYE, R_EYE, L_PUP, R_PUP = range(0, 24), range(24, 48), 197, 198


# -- the arithmetic, testable without a card -----------------------------------
def clean_spans(spans, length: float | None = None) -> list[tuple[float, float]]:
    out = []
    for s in spans or []:
        try:
            a, b = float(s[0]), float(s[1])
        except (TypeError, ValueError, IndexError):
            continue
        if length:
            a, b = max(0.0, min(a, length)), max(0.0, min(b, length))
        if b - a >= 0.2:
            out.append((a, b))
    return sorted(out)


def ease_weight(t: float, spans: list[tuple[float, float]], ease: float = EASE) -> float:
    """How much of the correction applies at *t*: 1 inside a stretch, rising
    and falling over *ease* seconds around it, 0 elsewhere. No stretches: the
    whole clip."""
    if not spans:
        return 1.0
    best = 0.0
    for a, b in spans:
        if a - ease < t < b + ease:
            best = max(best, min(1.0, (t - (a - ease)) / ease, ((b + ease) - t) / ease))
    return best


def open_weight(openness: float, usual: float) -> float:
    """1 for eyes about as open as usual, fading to 0 as they close (from 80%
    of usual to 50%) and as they open wide (from 135% to 160%)."""
    if not usual or not np.isfinite(openness):
        return 0.0
    r = openness / usual
    return float(np.clip((r - 0.5) / 0.3, 0, 1) * np.clip((1.6 - r) / 0.25, 0, 1))


def smooth_gaze(gaze: np.ndarray, good: np.ndarray, fps: float, sigma: float = SMOOTH) -> np.ndarray:
    """The gaze with blinks and frames without a face filled in from their
    neighbours, then smoothed: the slow drift, not the quick movements."""
    n = len(gaze)
    idx = np.arange(n)
    out = np.zeros((n, 2))
    if not good.any():
        return out + LOOK
    for k in range(2):
        out[:, k] = np.interp(idx, idx[good], gaze[good, k])
    sig = max(1.0, sigma * fps)
    half = int(3 * sig)
    ker = np.exp(-0.5 * (np.arange(-half, half + 1) / sig) ** 2)
    ker /= ker.sum()
    for k in range(2):
        out[:, k] = np.convolve(np.pad(out[:, k], half, mode="edge"), ker, mode="valid")
    return out


def shifts_for(smoothed: np.ndarray, gain: np.ndarray) -> np.ndarray:
    """The keypoint shift per frame that brings the smoothed gaze to the lens:
    none inside the dead band, none in a direction with too little response,
    and never more than CAP."""
    err = LOOK - smoothed
    err[np.abs(err) < DEAD] = 0.0
    use = gain >= MIN_GAIN
    shift = np.where(use, err / np.where(use, gain, 1.0), 0.0)
    return np.clip(shift, -CAP, CAP)


def gaze_of(lmk: np.ndarray) -> tuple[float, float, float]:
    """(x, y, open) from LivePortrait's 203 landmarks: the pupils' offset from
    their eyes' centres, in eye widths, and how open the eyes are."""
    parts = []
    for (a, b), (u, d), pup in (((0, 12), (6, 18), L_PUP), ((24, 36), (30, 42), R_PUP)):
        c = (lmk[a] + lmk[b]) / 2
        w = float(np.linalg.norm(lmk[a] - lmk[b])) + 1e-6
        parts.append(((lmk[pup] - c) / w, float(np.linalg.norm(lmk[u] - lmk[d])) / w))
    g = (parts[0][0] + parts[1][0]) / 2
    return float(g[0]), float(g[1]), (parts[0][1] + parts[1][1]) / 2


# -- the model ------------------------------------------------------------------
def ensure_weights(emit) -> None:
    missing = [f for f in WEIGHT_FILES if not (WEIGHTS / f).is_file()]
    if not missing:
        return
    emit(0.01, "downloading")
    from huggingface_hub import hf_hub_download  # noqa: PLC0415
    for f in missing:
        hf_hub_download("KwaiVGI/LivePortrait", f, local_dir=str(WEIGHTS))


class Model:
    def __init__(self):
        sys.path.insert(0, str(LP_ROOT))
        import cv2  # noqa: PLC0415
        from src.config.crop_config import CropConfig  # noqa: PLC0415
        from src.config.inference_config import InferenceConfig  # noqa: PLC0415
        from src.live_portrait_wrapper import LivePortraitWrapper  # noqa: PLC0415
        from src.utils.crop import paste_back, prepare_paste_back  # noqa: PLC0415
        from src.utils.cropper import Cropper  # noqa: PLC0415
        self.cv2, self.paste_back, self.prepare_paste_back = cv2, paste_back, prepare_paste_back
        self.crop = CropConfig()
        self.crop.scale = CROP_SCALE
        self.wrap = LivePortraitWrapper(inference_cfg=InferenceConfig())
        self.cropper = Cropper(crop_cfg=self.crop)

    def analyse(self, rgb):
        """(crop info, (x, y, open)), or None without a face."""
        try:
            info = self.cropper.crop_source_image(rgb, self.crop)
        except Exception:                                  # noqa: BLE001 -- a frame without a usable face
            return None
        return (info, gaze_of(info["lmk_crop"])) if info is not None else None

    def render(self, info, dx: float, dy: float):
        w = self.wrap
        src = w.prepare_source(info["img_crop_256x256"])
        kp = w.get_kp_info(src)
        f3d = w.extract_feature_3d(src)
        x_s = w.transform_keypoint(kp)
        e = kp["exp"]
        e[0, 11, 0] += dx; e[0, 15, 0] += dx
        e[0, 11, 1] += dy; e[0, 15, 1] += dy
        x_d = w.stitching(x_s, w.transform_keypoint(kp))
        return w.parse_output(w.warp_decode(f3d, x_s, x_d)["out"])[0]

    def measured(self, info, dx: float, dy: float):
        a = self.analyse(self.render(info, dx, dy))
        return a[1] if a else None

    def gain(self, sample, toward: np.ndarray) -> np.ndarray:
        """Eye widths the pupils move per unit of keypoint shift, on a few
        open-eyed frames, probed in the direction the correction will go."""
        probe = np.where(np.abs(toward) > 0.005, np.sign(toward) * 0.015, 0.015)

        def median(dx, dy):
            got = [g for g in (self.measured(i, dx, dy) for i in sample) if g]
            return np.median(np.array(got)[:, :2], 0) if got else None

        base = median(0.0, 0.0)
        gain = np.zeros(2)
        for k in range(2):
            d = [0.0, 0.0]
            d[k] = float(probe[k])
            moved = median(*d)
            if base is not None and moved is not None:
                gain[k] = (moved[k] - base[k]) / probe[k]
        return gain

    def corrected(self, frame, info, dx: float, dy: float):
        cv2 = self.cv2
        out = self.render(info, dx, dy)
        blur = cv2.GaussianBlur(out, (0, 0), 1.6)
        out = cv2.addWeighted(out, 1 + SHARPEN, blur, -SHARPEN, 0)
        size = out.shape[0]
        # The landmarks are in the frame's coordinates; the mask is drawn in the crop's.
        lmk = info["lmk_crop"]
        lc = np.hstack([lmk, np.ones((len(lmk), 1))]) @ info["M_o2c"][:2].T * size / self.crop.dsize
        mask = np.zeros((size, size), np.float32)
        for eye in (L_EYE, R_EYE):
            pts = lc[list(eye)]
            c = pts.mean(0)
            cv2.fillConvexPoly(mask, cv2.convexHull(((pts - c) * 1.6 + c).astype(np.int32)), 1.0)
            cv2.circle(mask, tuple(int(v) for v in c), int(np.linalg.norm(pts[0] - pts[12]) * 0.55), 1.0, -1)
        k = max(3, int(size * 0.03)) | 1
        mask = (np.dstack([cv2.GaussianBlur(mask, (k * 3, k * 3), k)] * 3) * 255).astype(np.uint8)
        h, w = frame.shape[:2]
        return self.paste_back(out, info["M_c2o"], frame, self.prepare_paste_back(mask, info["M_c2o"], dsize=(w, h)))


# -- the job --------------------------------------------------------------------
def _frames(path: str):
    import cv2  # noqa: PLC0415
    cap = cv2.VideoCapture(path)
    try:
        while True:
            ok, f = cap.read()
            if not ok:
                return
            yield cv2.cvtColor(f, cv2.COLOR_BGR2RGB)
    finally:
        cap.release()


def run(src: str, dst: str, spans, strength: float = 1.0, emit=None) -> dict:
    emit = emit or (lambda p, phase: None)
    import cv2  # noqa: PLC0415
    cap = cv2.VideoCapture(src)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if n <= 0 or not w:
        raise RuntimeError("the camera could not be read")
    spans = clean_spans(spans, n / fps)
    ensure_weights(emit)
    emit(0.03, "loading")
    model = Model()
    # Pass one: where the eyes point, on the frames the stretches reach (and
    # the smoothing's reach around them).
    reach = EASE + 3 * SMOOTH
    wanted = np.array([ease_weight(i / fps, spans, reach) > 0 for i in range(n)])
    gaze = np.full((n, 2), np.nan)
    openness = np.full(n, np.nan)
    kept: list[tuple[int, dict]] = []
    todo = max(1, int(wanted.sum()))
    seen = 0
    for i, rgb in enumerate(_frames(src)):
        if i >= n:
            break
        if not wanted[i]:
            continue
        a = model.analyse(rgb)
        seen += 1
        if a:
            gaze[i], openness[i] = a[1][:2], a[1][2]
            if len(kept) < 40 and i % max(1, todo // 40) == 0:
                kept.append((i, a[0]))
        if seen % 30 == 0:
            emit(0.05 + 0.3 * seen / todo, "measuring")
    have = np.isfinite(openness)
    if not have.any():
        raise RuntimeError("no face was found in the camera" + (" in those stretches" if spans else ""))
    usual = float(np.nanmedian(openness))
    good = have & (openness > 0.7 * usual) & (openness < 1.35 * usual)
    if not good.any():
        good = have
    smoothed = smooth_gaze(np.nan_to_num(gaze), good, fps)
    sample = [info for i, info in kept if good[i]]
    sample = sample[:: max(1, len(sample) // 6)][:6] or [info for _, info in kept[:6]]
    emit(0.36, "measuring")
    gain = model.gain(sample, LOOK - np.median(smoothed[good], 0))
    shift = shifts_for(smoothed, gain)
    # Pass two: rendered where a stretch and the eyes allow; every other frame
    # is the recording's own.
    from .media import X265  # noqa: PLC0415
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                           "-s", f"{w}x{h}", "-r", f"{fps:.6f}", "-i", "-", "-an", *X265,
                           "-movflags", "+faststart", dst], stdin=subprocess.PIPE)
    changed = 0
    try:
        for i, rgb in enumerate(_frames(src)):
            if i >= n:
                break
            wt = ease_weight(i / fps, spans) * strength
            if wt > 0 and (shift[i] != 0).any() and have[i]:
                a = model.analyse(rgb)
                if a:
                    wt *= open_weight(a[1][2], usual)
                    if wt > 0.01:
                        rgb = model.corrected(rgb, a[0], float(shift[i, 0] * wt), float(shift[i, 1] * wt))
                        changed += 1
            ff.stdin.write(np.ascontiguousarray(rgb).tobytes())
            if i % 30 == 0:
                emit(0.38 + 0.6 * i / n, "rendering")
    finally:
        ff.stdin.close()
        ff.wait()
    if ff.returncode != 0:
        raise RuntimeError("the corrected camera could not be written")
    before = np.median(smoothed[good], 0)
    return {"frames": n, "changed": changed, "gaze": [round(float(v), 3) for v in before],
            "shift": [round(float(v), 4) for v in np.median(shift[good], 0)],
            "gain": [round(float(v), 2) for v in gain]}


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text())
    # LivePortrait and its face finder talk on stdout; the manager reads
    # this process's stdout as its reports.
    real = sys.stdout
    sys.stdout = sys.stderr

    def emit(progress: float, phase: str) -> None:
        print(json.dumps({"progress": round(progress, 3), "phase": phase}), file=real, flush=True)

    try:
        result = run(job["src"], job["dst"], job.get("spans") or [], float(job.get("strength") or 1.0), emit)
    except Exception as exc:                               # noqa: BLE001 -- reported to the manager
        print(json.dumps({"error": str(exc)[:400]}), file=real, flush=True)
        return 1
    print(json.dumps({"done": result}), file=real, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
