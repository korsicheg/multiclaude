#!/usr/bin/env python3
"""Draw a Telegram sticker as one PNG to look at, and print what it is. The router runs it for each
sticker, with the Python of the add-in's own venv (stickers.PYTHON), so the router itself needs no packages.

  render.py STICKER PNG

A still sticker (.webp) comes out as it is. An animated (.tgs) or video (.webm) one comes out as a
grid of up to 9 numbered frames from start to end, since the motion often carries the feeling.
Prints {"kind": "still" | "animated" | "video", "seconds": 3.0, "frames": 9}."""
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FRAMES = 9
COLUMNS = 3
CELL = 256
GAP = 8
BACKGROUND = (238, 238, 238)  # light grey, so a sticker's white outline still shows
NUMBER = (120, 120, 120)


@dataclass
class Sticker:
    kind: str
    frames: list  # PIL images, start to end
    seconds: float | None = None


def evenly(items, count=FRAMES):
    """count items spread evenly from the first to the last."""
    if len(items) <= count:
        return list(items)
    return [items[round(i * (len(items) - 1) / (count - 1))] for i in range(count)]


def video(path):
    import imageio_ffmpeg

    count, seconds = imageio_ffmpeg.count_frames_and_secs(str(path))
    wanted = set(evenly(range(count)))
    # libvpx keeps a video sticker's transparency; ffmpeg's own VP9 decoder drops it
    reader = imageio_ffmpeg.read_frames(str(path), pix_fmt="rgba", bits_per_pixel=32,
                                        input_params=["-c:v", "libvpx-vp9"])
    size = next(reader)["size"]
    frames = [Image.frombytes("RGBA", size, raw) for index, raw in enumerate(reader) if index in wanted]
    return Sticker("video", frames, seconds)


def animated(path):
    from rlottie_python import LottieAnimation

    animation = LottieAnimation.from_tgs(str(path))
    numbers = evenly(range(animation.lottie_animation_get_totalframe()))
    frames = [animation.render_pillow_frame(frame_num=number) for number in numbers]
    return Sticker("animated", frames, animation.lottie_animation_get_duration())


def still(path):
    return Sticker("still", [Image.open(path).convert("RGBA")])


READERS = {".webm": video, ".tgs": animated}


def picture(frames):
    return on_background(frames[0]) if len(frames) == 1 else grid(frames)


def on_background(frame):
    sheet = Image.new("RGB", frame.size, BACKGROUND)
    sheet.paste(frame, (0, 0), frame)
    return sheet


def grid(frames):
    columns = min(COLUMNS, len(frames))
    rows = math.ceil(len(frames) / columns)
    sheet = Image.new("RGB", (columns * (CELL + GAP) - GAP, rows * (CELL + GAP) - GAP), BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=CELL // 10)
    for number, frame in enumerate(frames, start=1):
        row, column = divmod(number - 1, columns)
        left, top = column * (CELL + GAP), row * (CELL + GAP)
        cell = frame.copy()
        cell.thumbnail((CELL, CELL))
        sheet.paste(cell, (left + (CELL - cell.width) // 2, top + (CELL - cell.height) // 2), cell)
        draw.text((left + 6, top + 4), str(number), fill=NUMBER, font=font)
    return sheet


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__.split("\n\n")[1].strip())
    source, out = Path(sys.argv[1]), Path(sys.argv[2])
    sticker = READERS.get(source.suffix.lower(), still)(source)
    picture(sticker.frames).save(out)
    seconds = round(sticker.seconds, 1) if sticker.seconds else None
    print(json.dumps({"kind": sticker.kind, "seconds": seconds, "frames": len(sticker.frames)}))


if __name__ == "__main__":
    main()
