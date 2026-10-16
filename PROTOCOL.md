# xTool P2S network protocol (working notes)

Sources: xTool Studio 1.7.30 `app.asar` (`atomm-sharedworker.js`, editor `src.*.js`),
cross-checked with the open-source ha-xtool project. Items marked **unverified** have
not been confirmed on a real P2S yet.

## Transport
- TLS WebSocket, port **28900**, self-signed certificate (Studio skips verification).
- Studio opens three sockets with the same session id:
  `wss://<ip>:28900/websocket?id=<ms timestamp>&function=instruction|file_stream|media_stream`
- Only one controller at a time: close xTool Studio before connecting.

## Frame envelope (every message, binary WS frames)
| bytes | meaning |
|---|---|
| 0-1 | `BA BE` magic |
| 2-4 | payload length, big-endian |
| 5 | protocol type (low 7 bits); bit 7 set = payload CRC skipped |
| 6-7 | CRC-16/ARC of payload (0 if skipped) |
| 8-9 | CRC-16/ARC of bytes 0-7 |

Protocol types: 4 = JSON (instruction channel), 33 = file transfer, 34 = media.
Readers buffer and resync byte-by-byte on bad magic or CRC.

## Instruction channel (JSON, type 4)
Request: `{"type":"request","method":"GET|PUT|POST","url":"/v1/...","params":{},"data":{},"timestamp":<ms>,"transactionId":<1..65500>}`
Response: `{"type":"response","code":0,"msg":"ok","transactionId":N,"data":{...}}`. Anything else is a push event.

1. **Handshake** (first message): `GET /v1/user/parity` with
   `data:{userID:"mk-guest", userKey:"bWFrZWJsb2NrLXh0b29s"}`. A non-zero code closes the session.
2. **Heartbeat**: every 3 s `GET /v1/user/ping` with fixed `transactionId 65510`. The device must answer within 11 s.
3. File channel heartbeat: payload `02` (type 33) every 5 s; the device replies `82`.

## P2S routes (differ from F1/F2)
| purpose | route |
|---|---|
| status | `GET /v1/device/runningStatus` |
| config read | `GET /v1/config/get` with `data:{alias:"config",type:"user",kv:[keys]}` |
| config write | `PUT /v1/config/set` |
| stats | `GET /v1/device/workingInfo` |
| camera still | `GET /v1/camera/image` `data:{stream:"0"|"1"}` → `{filename}` → download, filetype 5 |
| lid | `GET /v1/peripheral/param` `params:{type:"gap"}` |
| job control | `PUT /v1/processing/state` `params:{action:"start|pause|stop"}` (**unverified on P2S**) |

## File upload (confirmed in Studio source)
1. Instruction: `PUT /v1/filetransfer/upload` with
   `{filetype, filename, filesize, digesttype:1, digestdata:<md5 hex>, channel:<1-255>, packetsize:1048576}`.
   The reply may reduce `packetsize`.
2. **The device pulls the data.** It sends `FILE_REQUEST` on file_stream:
   `01 | channel | offset (5 B) | window (3 B)`.
3. The client answers with `FILE_DATA` packets, each up to packetsize:
   `81 | channel | offset (5 B) | bytes`, framed as type 33 **with the payload CRC disabled**.
4. Repeat until the device sends `{"url":"/v1/filetransfer/finish","data":{"code":0,"channel":N}}`.
   Non-zero code = failure. To abort, send `PUT /v1/filetransfer/finish {code:-9, channel}`.

Download is the reverse: `PUT /v1/filetransfer/download` → `{filesize, digestdata}`. The client sends
`FILE_REQUEST` windows (≤5 MiB), the device sends `FILE_DATA`, the client checks the MD5 and then sends `finish {code:0}`.

## Running a job (confirmed in xcs-ext-p2s 1.5.66)
1. File transfer `filetype:1` (PROCESSING). The P2S reports `gcodeFormat:["txt","bin"]`, and then Studio
   **always** sends BGC: `filename:"tmp.bgcode"`. Text (`tmp.gcode`) is only its "Import G-code" path.
   Tested: a text upload runs the motion but the laser doesn't fire.
2. `PUT /v1/processing/upload/config` `{fileType:"bin"|"txt", autoStart:0, taskId:<random UUID>}`.
3. The job starts from the machine's button. Studio has no "start" call for the P2S.
   Pause/stop: `PUT /v1/processing/state` `params:{action:"pause"|"stop"}`.

## BGC (binary G-code)
Byte packing of the G-code text, no header (Studio decoder `Yb` in the main process):
newline 10, space 192, `"` 194, `-` 205, `A`-`Z` 225-250, decimal point 206; digits are packed
two per byte (0x00-0x99). The integer part drops one leading 0 nibble; the fraction drops one trailing '0'.
Comment lines are 0xC3 ('#') followed by the comment's plain text up to the newline. Blank lines are dropped.
Our encoder re-creates a Studio-sent job byte for byte. Encoder/decoder in `p2s/bgc.py`.

## Raster blocks (photo / fill), from a job Studio sent to the laser
    # P2s BITMAP HEAD / G4 P0.1 / # motion_start
    # blockConfig={"powerFactor":0.08,"density":"12.00","power":[0,80],"bitmapMode":"Jarvis","isVector":false}
    M117S1 / M2519S2T0 (photo; S4 = fill) / M15 S64
    G1F<mm/min>S0                   once per block
    G0X<x - run-up>Y<y>  G0Z<z>     first row (every raster block repeats Z)
    G1X<first burn>                 laser-off run-up
    G1X<x>S<power> / G1X<x>S0       burns and gaps, pixel edges on the line grid
    G1X<last burn + run-up>S0       run-out
    G1Y<next y>                     next line, scanned back the other way (zMode)
    G1X<run-up start>               only if the run-out didn't already pass it
    # P2s BITMAP TAIL / # motion_end
Run-up = min(v^2 / (2 * 3200), distance to the bed edges). Rows are trimmed to their first and last burn.
Line pitch = 10 / density mm; density in blockConfig = lines per mm.

## Lid / key
`gap` state `"off"` = lid **closed** (Studio: `isCover = state === "off"`). `machine_lock` `"on"` = key in.

## P2S G-code (from the plugin's templates)
- File: `# date/version/gc` comment block, `G90`, `G0 F3000`.
- Head: `# WorkingInfo=` comments (mode, material, focalLen, speed, power), accessory commands,
  `M32X<accel>` (3200), `G0 F9600`, `G0 X0 Y0`, `M4 S0`, `G1 F1000`, `M19 S1`, `M18 S0`.
- Each vector element: `G4 P0.1`, `G90`, `# motion_start`,
  `# blockConfig={"powerFactor": <power%/100>, "isVector": true}`,
  `M117S1` (score) / `M117S3` (cut), `M2519S<1 score|3 cut|4 fill|2 bitmap>T0` (flat bed),
  `M15 S<round(max(10,air%)*2.55)>` (air assist), then moves, then `# motion_end`.
- Tail: `G0 S0`, `M3 S0`, `G0 Z0`, `G0 X0 Y0`, `G0 U0`, `G0 F10000`, `G1 F10000`, `G4 P0.1`, `M109 S0`, `M6 P1`.
- Units: power = percent × 10 (0-1000); speed = mm/s × 60 (mm/min).
- Honeycomb/flat mode: 600 × 305 mm, canvas mm = machine mm (origin top-left).
- **Move lines** (written by xTool's compiled planner, confirmed from a Studio export):
  `G0X<x>Y<y>` travel, then `G1X<x>Y<y>S<power>F<speed>` on the first cut move and plain
  `G1X<x>Y<y>` after that. No spaces; coordinates to 3 decimals with trailing zeros trimmed.
  The first travel is followed by `G0Z<focalLen to 3 dp>` and a repeat of the same `G0X..Y..`.
  Curves are 0.1 mm segments. Closed shapes start at the vertex nearest the head.
  Elements run back to back: each has its own VECTOR HEAD block, but there is only one
  `# P2s VECTOR TAIL` / `# motion_end` (before `# END`).
- Head also carries an accessory block, e.g. `M9064 B<fanGear>`, `M9039 C<purifierGear>`,
  `G198 P<port> "<M-code>"` lines that relay commands to accessories.
- Studio keeps the last generated job at
  `%APPDATA%\xTool Studio\atomm\preview\<id>\ZY013\task\` (gcode.txt, manifest.json, ...).

## Project file (.xcs)
Plain JSON. Shapes are in `canvas[].displays[]` (mm, `x/y/width/height/angle/scale`).
The per-machine process settings that are actually used are in `device.data.value[][1].displays.value`
(power %, speed mm/s, passes, air assist), with job settings under `data.HONEYCOMB_PANEL`
(material, thickness, focalLength, fan/purifier gears).

## Height measurement (Studio accurateMeasure, RULE_TWO)
1. `POST /v1/laser-head/control {action:"go_to", waitTime:30000, f:9600, x, y}` with
   x = target − IR offset X. The offset comes from config `laserMeasureOffset` ("X64.5Y1.4Z136.7"
   on this machine; plugin default 68).
   **On the real machine this left the laser over the target and the sensor ~60 mm to the right**,
   so the app moves the head a further 50 mm left (x = target − offset − 50; 60 mm overshot slightly),
   adjustable with the `ir_shift_x` setting. (Studio also adds half its 45 × 30 marker icon, `+22.5, +15`; how its click
   point relates to the icon isn't known.)
2. Fill light off `PUT /v1/peripheral/param {type:fill_light} {action:"set_bri", idx:0, value:0}`;
   IR LED on `{type:ir_led} {action:"on", index:1}`.
3. `PUT /v1/peripheral/param {type:ir_measure_distance} {action:"get_distance", type:"single"}`
   returns `{coord_x, coord_y, distance}`.
4. IR off, lights idx 2 and 3 to 255, head home `go_to x0 y0`.
The `ir_led` index 1 is the head's visible red measuring laser. The head camera is stream `"1"` of
`/v1/camera/image` (Studio `captureLocalImage`; stream `"0"` is the lid camera), so the app can photograph
the red dot before measuring to show where the reading is taken.
In honeycomb mode, focus Z = 25 − distance. Valid range −50…25.

## Camera
`GET /v1/camera/image {stream:"0"|"1"}` returns a filename, then a file_stream download (filetype 5) of a
4656×3496 JPEG. The calibration comes from `GET /v1/file/content {type:"global_calib.json"}`: a 62×32 grid of
source pixels for bed points at 10 mm spacing (output 2440×1220 = 4 px/mm), calibrated at thick0 = 3 mm.
Height correction (fitted from a test burn, residual 0.1 mm): bed point P is sampled at
C + (P − C)·k, with C ≈ (229, −155) mm and k = 348 / (348 + distance − 3).

## Rotary attachment (RA2, LASER_CYLINDER mode)
Checked against a roller job Studio sent to the laser (fetched back with `p2s usb fetch`).
- Header comments: `gc={"size":{"w":<377+axisY>,"h":500}}`, `gc={"offset":{"x":0,"y":50}}`,
  `gc={"start":{"x":0,"y":<axisY>.0000}}`, `gc={"keys":["u","x"],"rm":-1}` (roller; chuck rm 1),
  `timeConfig` vectorAccel 100. `WorkingInfo` mode `LASER_CYLINDER`.
- Head ends with `M535U<factor>` and `G0Y<axisY>` (the head parks over the object's top line).
  Factor (plugin `aze`): roller = `rollerMultiple` 0.59917; chuck = 128 / circumference.
  RA3: ×4 and a different code path (not supported here).
- Elements use tool `T1`: `M2519S1T1`. Bitmap head gets `G0 Y<axisY>` (plugin `bitmapHead` startY).
- Moves: travel is `G0X<x>` then `G0U<u>` on separate lines; first travel then `G0Z<z>` and both again.
  Burns are `G1U<u>X<x>` (U first), `S`/`F` on the first move of each outline. Tail has `G0 U0`.
- Mapping (plugin `Pct`, non-RA3 branch): Studio's cylinder canvas is the bed turned 90°;
  canvas x → `U = rm·(x − centralAxisPosition)`, canvas y → `X = y + 50`. `centralAxisPosition` is the
  axis line the user drags on the canvas (default 125); the head parks at `Y = 250 − centralAxisPosition`.
  In a bed-aligned view (x = machine X, y = machine Y) this is simply `X = x`, `U = s·(y − axisY)`,
  s = +1 rollers, −1 chuck. Studio's work area is on the back side of the line (y ≤ axisY),
  from X 50 to 550.
- The planner's first outline starts at the vertex nearest Studio's canvas origin
  (X 50, y 250 in the bed view), then nearest-neighbour as on the flat bed.
- This app's default "centre" alignment is not a Studio feature: it only changes the U origin
  (U = s·(y − design centre)), so the design's middle is at U = 0 when the job starts; the
  machine sees ordinary negative and positive U moves, as in Studio's own jobs.

## Moving the head and the red pointer
- Move: `POST /v1/laser-head/control {action:"go_to", waitTime:30000, f:9600, x, y}`; position:
  `{action:"get_coord", waitTime:0}`. Studio's optics test also jogs with `POST /v1/cnc/cmd`
  `params:{cmd:"G91 X<dx> Y<dy> F9600"}` (raw G-code).
- Red pointer down the beam path ("coaxial"): `PUT /v1/peripheral/param` `params:{type:"coaxial_Ir"}`
  `data:{action:"on"|"off"}`. (The measuring dot is a different light: `ir_led` index 1.)
- Unconfirmed whether `go_to` x/y is exactly where the beam lands; the app has an optional
  correction (`pointer_dx` / `pointer_dy` settings).
