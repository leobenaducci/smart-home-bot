// The house cameras: a list, and one still at a time.
//
// Snapshots, never the stream. `/stream/<cam>` is MJPEG and HomeCameras signals
// the *previous* generator to exit when a new one opens — one viewer per
// camera. A panel holding a stream would silently kick whoever was watching
// that camera in the browser, which is the kind of bug nobody ever traces back
// to the wall.
//
// The `?w=`/`?q=` parameters this uses were added to HomeCameras for this
// panel: a full frame off these cameras is 130-175 KB, and at w=640&q=60 it is
// about 40 KB.
#pragma once

#include <Arduino.h>
#include <lvgl.h>

struct Camera {
  String id;
  String name;
};

#define CAMERAS_MAX 8

// Register the JPEG decoder callback. Call once, before anything else here.
void camerasBegin();

// GET /api/cameras. No auth — the camera server asks for none on this route.
bool camerasFetchList();

uint16_t camerasCount();
const Camera* camerasAt(uint16_t i);

// Fetch one snapshot and decode it into `canvas`, letterboxed and centred.
//
// Letterboxed because the served frame is the *rotated* one: a camera with
// rotation 90 hands back a portrait image whose aspect ratio does not match the
// width and height the registry advertises. Fitting to what actually arrives is
// the only thing that works for both.
bool camerasDrawSnapshot(uint16_t i, lv_obj_t* canvas);
