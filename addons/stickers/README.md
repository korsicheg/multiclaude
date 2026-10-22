# Stickers

A Telegram sticker reaches the person's assistant as a picture it can look at, not only as its emoji, since a sticker's face, gesture and motion say more. A still sticker is drawn as it is; an animated or video one as a grid of up to 9 numbered frames from start to end, and the assistant is told how long it runs. A sticker waits its turn in the person's queue, so messages sent before it aren't held up. If fetching or drawing fails, the assistant gets the sticker as before: its emoji and `file_id`.

The pictures are drawn with [Pillow](https://python-pillow.org), [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) (video stickers, with ffmpeg's static build) and [rlottie-python](https://github.com/laggykiller/rlottie-python) (animated ones), in a venv of their own outside the repo, so the bot itself stays standard-library only. A sticker takes about a second.

## What it needs

- [uv](https://docs.astral.sh/uv/), which installs Python 3.12 and the libraries into the venv.
- About 100 MB of disk for the venv.

## Setup

`assistant addon enable stickers`. Its `setup` script makes the venv in `~/.local/share/stickers` (`ASSISTANT_STICKERS` moves it); by hand, that is:

```sh
uv venv --python 3.12 ~/.local/share/stickers/venv
uv pip install --python ~/.local/share/stickers/venv/bin/python pillow==13.0.0 imageio-ffmpeg==0.6.0 rlottie-python==1.3.8
```

The pictures go in `~/.local/share/stickers/pictures`, outside the backup, and are deleted after 7 days.

## Files

- `lib/multiclaude/addons/stickers.py`: queueing a sticker, fetching it, drawing it and listing the picture under the message's Attachments.
- `addons/stickers/setup`: run by `assistant addon enable stickers`; makes the venv.
- `addons/stickers/render.py`: run with the venv; draws a sticker as one PNG and prints what it is.
