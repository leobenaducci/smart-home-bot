# Board Settings — camera-server

Target: **ESP32-S3-WROOM-1 N16R8** (16 MB QIO flash + 8 MB QSPI PSRAM), CH343 USB-serial bridge (COM3).

Verified from the chip on 2026-08-10: ESP32-S3 rev v0.2, MAC `30:ED:A0:BD:67:B4`,
16 MB flash (quad), eFuse `PSRAM_CAP=8M` / vendor AP 3.3 V (→ QSPI PSRAM).

## Arduino IDE 2.x — Tools menu

Select **Board → esp32 → "ESP32S3 Dev Module"**, then set:

| Tools menu item | Value |
|---|---|
| USB CDC On Boot | **Disabled** |
| CPU Frequency | **240MHz (WiFi)** |
| Core Debug Level | **None** |
| USB DFU On Boot | **Disabled** |
| Erase All Flash Before Sketch Upload | **Disabled** |
| Events Run On | **Core 1** |
| Flash Mode | **QIO 80MHz** |
| Flash Size | **16MB (128Mb)** |
| JTAG Adapter | **Disabled** |
| Arduino Runs On | **Core 1** |
| USB Firmware MSC On Boot | **Disabled** |
| Partition Scheme | **Custom** |
| PSRAM | **QSPI PSRAM** |
| Upload Mode | **UART0 / Hardware CDC** |
| Upload Speed | **921600** |
| USB Mode | **Hardware CDC and JTAG** |
| Zigbee Mode | **Disabled** |
| Port | **COM3** |

### The three that actually matter (were wrong before)
- **PSRAM → QSPI PSRAM** (was Disabled) — required for the camera to allocate full-res JPEG framebuffers.
- **Flash Size → 16MB** (was 4MB) — chip is 16 MB.
- **Flash Mode → QIO 80MHz** — flash is quad-capable.

"Partition Scheme → Custom" makes the IDE use the `partitions.csv` in this folder (16 MB: dual-OTA + FATFS + `fr` + coredump). Original 4 MB table is saved as `partitions.csv.orig`.

## Equivalent FQBN (for arduino-cli)

```
esp32:esp32:esp32s3:UploadSpeed=921600,USBMode=hwcdc,CDCOnBoot=default,MSCOnBoot=default,DFUOnBoot=default,UploadMode=default,CPUFreq=240,FlashMode=qio,FlashSize=16M,PartitionScheme=custom,DebugLevel=none,PSRAM=enabled,LoopCore=1,EventsCore=1,EraseFlash=none,JTAGAdapter=default,ZigbeeMode=default
```

## Notes
- After enabling PSRAM / changing flash size the first time: if it misbehaves, do one upload with
  **Erase All Flash Before Sketch Upload → Enabled**, then switch it back to **Disabled**.
- `sketch.yaml` in this folder holds the same settings as a profile. Arduino IDE only reads it on a
  fresh open; if the IDE ignores it, use the manual table above instead (or delete `sketch.yaml`).
