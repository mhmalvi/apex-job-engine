"""Attach a file over raw CDP without letting Chromium's native chooser open.

EXCERPT — original modules: ``file_upload`` (whole, minus the Google Picker
path) and the ``_press`` helper from ``apply_runner`` (APEX browser apply lane).
Removed: the Google Forms Picker variant (same idea: find the picker iframe's
``<input type=file>`` in a pierced DOM snapshot and write it with
``DOM.setFileInputFiles``), and the runner code that measures where the
upload control sits.

WHY THIS EXISTS

The GTK "Open File" dialog is MODAL. While it is up the page receives zero
pointer events, so every click the fill makes afterwards is a no-op. This
stops the dialog being drawn at all: with ``Page.setInterceptFileChooserDialog``
armed, Chromium raises ``Page.fileChooserOpened`` instead, naming the backend
node that asked for a file, and ``DOM.setFileInputFiles`` answers it with a
trusted write that fires the page's own input/change events.

It also reaches uploads a content script cannot: a scripted write needs an
``<input type=file>`` it can find, while many ATS widgets offer a button that
opens a chooser. CDP works from the node the chooser itself names.

The click that opens the chooser is a CDP ``Input.dispatchMouseEvent`` pair:
viewport coordinates, delivered straight to the renderer, carrying a user
gesture that a scripted ``el.click()`` does not always have. No xdotool, no
screen offsets, no wrong-DISPLAY faults.
"""

import asyncio
import json
import os

#: Arms and disarms interception. While armed, Chromium raises an event
#: instead of drawing the dialog.
INTERCEPT = "Page.setInterceptFileChooserDialog"

#: Raised in place of the dialog, carrying the node that asked for the file.
CHOOSER_EVENT = "Page.fileChooserOpened"

#: A chooser wanting a folder cannot be satisfied with a file path.
DIRECTORY_MODE = "openDirectory"


async def press(session, point):
    """A left click at viewport ``point`` ({'x', 'y'}), delivered to the renderer."""
    for kind in ("mousePressed", "mouseReleased"):
        await session.send("Input.dispatchMouseEvent", {
            "type": kind, "x": point["x"], "y": point["y"],
            "button": "left", "clickCount": 1,
        })


async def attach(session, path, *, open_chooser, timeout=15.0):
    """Hand one file to whatever control opens a chooser.

    Args:
        session: A CDP session exposing ``send(method, params)`` and
            ``wait_event(name, timeout)``.
        path: Absolute path of the file to attach.
        open_chooser: Awaitable that clicks the control which opens the
            chooser. Called only once interception is armed — arming after the
            click is too late, because the dialog is already drawn.
        timeout: Seconds to wait for the chooser event.

    Returns:
        ``{'ok': bool, 'note': str}``, plus ``backend_node_id`` when attached.
    """
    if not os.path.isfile(path):
        # Never tell the browser to attach something we do not have: it would
        # report success against an empty selection.
        return {"ok": False, "note": f"no file at {path}"}

    await session.send("Page.enable")
    await session.send(INTERCEPT, {"enabled": True})
    try:
        await open_chooser()
        try:
            event = await session.wait_event(CHOOSER_EVENT, timeout)
        except asyncio.TimeoutError:
            return {"ok": False,
                    "note": f"no file chooser opened within {timeout:.0f}s — "
                            "the control probably is not an upload"}

        mode = event.get("mode", "")
        if mode == DIRECTORY_MODE:
            return {"ok": False, "note": "the page asked for a directory, not a file"}

        node = event.get("backendNodeId")
        if not node:
            return {"ok": False, "note": "the file chooser named no node"}

        await session.send("DOM.setFileInputFiles",
                           {"files": [path], "backendNodeId": node})
        return {"ok": True, "note": "", "backend_node_id": node}
    finally:
        # Leaving interception armed silently breaks every later upload on this
        # page — including one a human would try to do by hand.
        await session.send(INTERCEPT, {"enabled": False})


class CdpSession:
    """One CDP conversation over an open websocket.

    Opening a socket per call cannot work here: arming interception, clicking,
    and catching the event have to happen on the SAME connection, or the event
    is raised on a socket nobody is listening to.

    Replies and events share the wire, so frames that are not the reply being
    waited on are parked rather than dropped.

    Args:
        socket: An open websocket exposing ``send(str)`` and ``recv()``
            (in production, a ``websockets`` client connection).
    """

    def __init__(self, socket):
        self._socket = socket
        self._next_id = 0
        self._events = []

    async def send(self, method, params=None):
        """Send one command and return its ``result`` object."""
        self._next_id += 1
        message_id = self._next_id
        await self._socket.send(json.dumps({
            "id": message_id, "method": method, "params": params or {},
        }))
        while True:
            frame = json.loads(await self._socket.recv())
            if frame.get("id") == message_id:
                return frame.get("result", {})
            if "method" in frame:
                self._events.append(frame)

    async def wait_event(self, name, timeout):
        """Wait for one event, honouring any already parked.

        Raises:
            asyncio.TimeoutError: If the event does not arrive in time.
        """
        for index, frame in enumerate(self._events):
            if frame.get("method") == name:
                return self._events.pop(index).get("params", {})
        return await asyncio.wait_for(self._read_until(name), timeout)

    async def _read_until(self, name):
        """Read frames until the named event arrives, parking the others."""
        while True:
            frame = json.loads(await self._socket.recv())
            if frame.get("method") == name:
                return frame.get("params", {})
            if "method" in frame:
                self._events.append(frame)
