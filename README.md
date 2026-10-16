# BetterLaser
A better laser software compared to the XTool software. Only for the P2S at the moment. Runs in browser.

Design and run jobs on an xTool P2S without xTool Studio.
Not affiliated with or endorsed by xTool. Pure Python 3.9+, nothing to install.
Close xTool Studio first: only one program can control the laser at a time.

## Start the app
Double-click **p2s-app.bat** (or run `p2s gui`). A small window opens, plus the app in your
browser at http://127.0.0.1:8765. Keep the small window open while you work, and close it to quit.
The app only listens on this computer.

Then click **Connect** (the address box says `usb` for the USB-C cable).

## What it does
- **Shapes:** rectangles, ellipses, lines (Shift snaps to 45°), polygons, stars, rounded
  rectangles and hearts, text from any installed TrueType font, imported SVG files,
  and rectangles/circles from xTool .xcs projects.
- **Text around a shape:** pick *Wrap around* in the text panel to set text over the top of a
  circle, ellipse, rectangle or any drawn shape (or inside, along the bottom). Drag the text to
  slide it round; it follows the shape when you move or resize it. Set the shape to *Off* to
  use it only as a guide. Move, resize, rotate; Ctrl+Z undo; Ctrl+D duplicate.
- **Per-object laser settings:** Score, Cut, Fill, Image or Off, with power, speed, passes,
  air assist, and lines per cm for fill/pictures. Jobs always run pictures, then fills, then
  scores, then cuts; smaller cuts (holes) go before the outlines around them.
- **Pictures:** grayscale (power follows the shade between min and max power) or dithering
  (Jarvis, Floyd–Steinberg, Stucki, Atkinson, Sierra, Bayer, threshold), plus brightness,
  contrast, gamma and invert. The preview shows the exact pixels that get engraved.
- **Camera:** takes a photo with the lid camera and straightens it onto the bed using the
  machine's own calibration, corrected for material height.
- **Measure:** click a spot and the head's IR sensor measures the material; focus Z is set
  automatically (Z = 25 − distance). *Show the spot* first takes a head-camera photo with the
  red measuring laser on, circles the dot, and lets you nudge it onto your part before measuring.
- **Rotary (RA2, 4-pin port):** tick *Rotary attachment* in the Job panel, choose rollers or
  chuck, enter the object's diameter and click *Set on bed* on the object's top line (the laser
  line). By default the **centre** of the design is engraved on the top of the object and half of
  it wraps each way, so you line up the object's centre line with the laser instead of guessing
  from the top edge as Studio does. *Line up by: Edge* keeps Studio's behaviour. One full turn is
  shown on the bed. Rotary projects from Studio (.xcs) import with their settings.
- **Laser head (crosshair button on the left):** a pop-out panel to jog the head (0.5–50 mm steps), send it home, switch the red pointer
  on/off, or click *Move to a point* and then the bed. *Show job start* parks the red pointer
  where the job will begin; *Trace job outline* runs it round the area the job covers, so you
  can line your part up before loading the job. The red pointer is coaxial with the beam.
- **Load job on laser** uploads it in the same binary format Studio uses. Then press **Start**
  on the machine. Pause and Stop are in the top bar.

## Status of each feature
- Score/cut G-code is matched to Studio's output and tested on the machine.
- Picture engraving reproduces a real Studio photo job line for line (73,073 lines, within 0.001 mm),
  and the binary files are byte-identical to Studio's format. Fill uses the same verified move format.
  Grayscale uses the same moves, with power varying per segment.
- Rotary score/cut on rollers reproduces a real Studio rotary job (every setup line identical,
  every point exact). Chuck mode uses the same code with Studio's chuck factor (128 / circumference)
  but hasn't been compared with a Studio chuck job. Fill and pictures on the rotary are marked
  experimental until checked against a Studio rotary photo job.
- Camera alignment is fitted from one test burn; use Camera > Align to fine-tune if needed.

## Command line
    p2s usb status | probe | watch | snapshot cam.jpg | calib
    p2s convert project.xcs out.gcode      (offline)
    p2s usb send project.xcs | job.gcode
    p2s usb pause | stop
    p2s usb fetch tmp.bgcode job.bgc       p2s decode job.bgc job.txt

## Layout
- `p2s/app.py` + `p2s/ui/index.html`  the Workbench app
- `p2s/design.py`  app design -> job; `p2s/fonts.py` TrueType text; `p2s/raster.py` fill/picture lines
- `p2s/gcode.py`  P2S G-code; `p2s/bgc.py` binary format; `p2s/device.py` laser connection
- `PROTOCOL.md`  everything learned about the P2S
- `tests/`  `python tests/test_*.py` (a fake P2S stands in for the real one)

## License
GPL-3.0, see LICENSE.
