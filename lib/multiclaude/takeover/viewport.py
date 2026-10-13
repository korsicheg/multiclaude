"""The browser's viewport, fitted to the screen the take-over page is on. The page reports the
space it has for the browser, in CSS pixels, whenever it opens or its window changes size, and the
viewport takes that size, so each frame shows 1:1 and fills a phone or a laptop window alike. The
scale stays the page's own: the stream's frames are CSS pixels at any scale, so a sharper one
wouldn't reach the screen. Once the take-over ends the viewport is put back as it was."""
import threading

from multiclaude.takeover.browser import Viewport

WIDTHS = range(320, 2561)   # CSS pixels a fitted viewport can have across
HEIGHTS = range(320, 1601)  # and down


class Fitting:
    def __init__(self, session):
        """session: the browser's agent-browser Session."""
        self.session = session
        self.original = None  # the viewport as it was, read just before it first changes
        self.current = None
        self.over = False
        self.lock = threading.Lock()  # one change at a time, in the order the page asked

    def fit(self, size):
        """Size the viewport to size ({"width", "height"}: the page's room for the browser), kept
        within WIDTHS and HEIGHTS. ValueError for a size that isn't one, NoBrowser if
        agent-browser fails. Once the take-over is over, a late size changes nothing."""
        width, height = fitted(size, "width", WIDTHS), fitted(size, "height", HEIGHTS)
        with self.lock:
            if self.over:
                return
            if self.original is None:
                self.original = self.current = self.session.viewport()
            wanted = Viewport(width, height, self.original.scale)
            if wanted != self.current:
                self.session.set_viewport(wanted)
                self.current = wanted

    def restore(self):
        """Put the viewport back as it was, and fit it no more; NoBrowser if agent-browser fails."""
        with self.lock:
            self.over = True
            if self.current != self.original:
                self.session.set_viewport(self.original)
                self.current = self.original


def fitted(size, axis, pixels):
    """size[axis] as whole CSS pixels within the range pixels."""
    try:
        value = round(float(size[axis]))
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError(f"a size needs a {axis}") from None
    return min(pixels[-1], max(pixels[0], value))
