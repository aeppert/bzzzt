#!/usr/bin/env python

import tornado.ioloop
import tornado.web
import tornado.websocket
import tornado.escape
import datetime
import functools
import logging

from gpiocrust import Header, OutputPin
from tornado.options import define, options

define("pin", default=8, help="output pin", type=int)
define("port", default=8888, help="run on the given port", type=int)
define("debug", default=False, help="run in debug mode", type=bool)

logger = logging.getLogger()


class Button(object):
    def __init__(self):
        self.presses = set()
        self._watchers = set()

    @property
    def is_pressed(self):
        return len(self.presses) > 0

    def has_changed_state(self):
        try:
            return self.is_pressed != self.latest_is_pressed
        except AttributeError:
            return True
        finally:
            self.latest_is_pressed = self.is_pressed

    def add_press(self, press):
        self.presses.add(press)
        self._invoke_watchers("add", press)

    def discard_press(self, press):
        self.presses.discard(press)
        self._invoke_watchers("discard", press)

    def add_watcher(self, method):
        self._watchers.add(method)

    def _invoke_watchers(self, method=None, changed=None):
        for watcher in self._watchers:
            watcher(method, changed, self)


class WebSocketHandler(tornado.websocket.WebSocketHandler):
    _PING_INTERVAL = 3.0
    _DISCONNECT_TIMEOUT = 10.0

    connections = set()
    button = Button()

    def open(self):
        self.id = self.get_argument("id")
        WebSocketHandler.connections.add(self)
        self._add_periodic_ping()
        self._add_cleanup_timeout()
        WebSocketHandler.broadcast_connections(self)

    def _add_periodic_ping(self):
        self._periodic_ping = tornado.ioloop.PeriodicCallback(
            functools.partial(self.ping, "0"), self._PING_INTERVAL * 1000.0)
        self._periodic_ping.start()

    def _add_cleanup_timeout(self):
        self.remove_timeout()
        io_loop = tornado.ioloop.IOLoop.instance()
        self._cleanup_timeout = io_loop.add_timeout(
            datetime.timedelta(seconds=self._DISCONNECT_TIMEOUT),
            functools.partial(WebSocketHandler.cleanup, self))

    def remove_timeout(self):
        io_loop = tornado.ioloop.IOLoop.instance()
        if hasattr(self, "_cleanup_timeout"):
            io_loop.remove_timeout(self._cleanup_timeout)
        

    def on_message(self, is_pressing):
        if is_pressing == "1":
            WebSocketHandler.button.add_press(self)
        else:
            WebSocketHandler.button.discard_press(self)

        if WebSocketHandler.button.has_changed_state():
            self.send_state()

    def on_pong(self, _):
        self._add_cleanup_timeout()

    def on_close(self):
        self.cleanup()

    def cleanup(self):
        self._periodic_ping.stop()
        self.remove_timeout()
        WebSocketHandler.button.discard_press(self)
        WebSocketHandler.connections.remove(self)
        WebSocketHandler.send_state(self)
        WebSocketHandler.broadcast_connections(self)

    def send_state(self):
        is_unlocked = WebSocketHandler.button.is_pressed
        data = {
            "is_unlocked": is_unlocked
        }
        if is_unlocked:
            data["id"] = self.id
        WebSocketHandler.broadcast_data(self, data)

    def broadcast_connections(self):
        WebSocketHandler.broadcast_data(self, {
            "connections": [c.id for c in WebSocketHandler.connections]
        })

    def broadcast_data(self, data):
        for connection in WebSocketHandler.connections:
            connection.write_message(tornado.escape.json_encode(data))


tornado.options.parse_command_line()

if not options.debug:
    syslog = logging.handlers.SysLogHandler(
        address=("logs.papertrailapp.com", 35157))
    logger.addHandler(syslog)

app = tornado.web.Application([
    (r"/", WebSocketHandler)
], debug=options.debug)

with Header() as header:
    finger = OutputPin(options.pin, value=False)

    @WebSocketHandler.button.add_watcher
    def update_pin(m, c, button):
        finger.value = button.is_pressed

    try:
        app.listen(options.port)
        tornado.ioloop.IOLoop.instance().start()
    except KeyboardInterrupt:
        pass
