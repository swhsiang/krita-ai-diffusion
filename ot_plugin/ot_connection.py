from __future__ import annotations
from enum import Enum
from PyQt5.QtCore import QObject, pyqtSignal, QUrl
from PyQt5.QtGui import QDesktopServices
import asyncio

from .ot_client import OTClient, ClientMessage, ClientEvent
from .settings import Settings, settings
from .properties import Property, ObservableProperties
from .resources import MissingResource
from .localization import translate as _
from . import util, eventloop

class ConnectionState(Enum):
    disconnected = 0
    connecting = 1
    connected = 2
    error = 3

class OTConnection(QObject, ObservableProperties):
    state = Property(ConnectionState.disconnected)
    error = Property("")

    state_changed = pyqtSignal(ConnectionState)
    error_changed = pyqtSignal(str)
    models_changed = pyqtSignal()
    ot_message_received = pyqtSignal(ClientMessage)

    def __init__(self):
        super().__init__()

        self._client: OTClient | None = None
        self._task: asyncio.Task | None = None

        settings.changed.connect(self._handle_settings_changed)
        self._update_state()

    def __del__(self):
        if self._task is not None:
            self._task.cancel()

    async def _connect(self, url: str):
        util.ot_client_logger.info(f"connection state: {self.state}")
        if self.state is ConnectionState.connected:
            await self.disconnect()
        self.error = None
        self.state = ConnectionState.connecting
        try:
            util.ot_client_logger.info(f"before call to OTClient.connect")
            self._client = await OTClient.connect(url)
            util.ot_client_logger.info(f"after call to OTClient.connect")
            if self._task is None:
                self._task = eventloop._loop.create_task(self._handle_messages())
            self.state = ConnectionState.connected
        except Exception as e:
            util.ot_client_logger.exception(e)
            # FIXME: not seeing this in the log
            self.error = util.ot_log_error(e)
            self.state = ConnectionState.error
    
    def connect(self):
        util.ot_client_logger.info(f"Connecting to OT server at {settings.ot_url}")
        eventloop.run(self._connect(settings.ot_url))

    async def disconnect(self):
        if self._task is not None:
            self._task.cancel()
            await self._task
            self._task = None

        self._client = None
        self.error = None
        self.state = ConnectionState.disconnected
        self._update_state()

    async def _handle_messages(self):
        client = self._client
        assert client is not None

        try:
            async for msg in client.listen():
                try:
                    self._handle_message(msg)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    util.ot_client_logger.exception(e)
                    self.error = _("Error handling server message: ") + str(e)
        except asyncio.CancelledError:
            pass  # shutdown

    def _handle_message(self, msg: ClientMessage):
        match msg:
            case (ClientEvent.error, "", *_):
                self.error = _("Error communicating with server: ") + str(msg.error)
            case (ClientEvent.disconnected, *_):
                self.error = _("Disconnected from server, trying to reconnect...")
            case (ClientEvent.connected, *_):
                self.error = ""
            case _:
                self.ot_message_received.emit(msg)

    def _update_state(self):
        if self.state in [ConnectionState.disconnected, ConnectionState.error]:
            self.state = ConnectionState.disconnected

    def _handle_settings_changed(self, key: str, value: object):
        self.error = ""
        eventloop.run(self.disconnect())
        self._update_state()
