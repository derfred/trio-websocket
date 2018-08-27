import enum
import itertools
import logging
import ssl

import trio
import wsproto.connection as wsconnection
import wsproto.events as wsevents
import wsproto.frame_protocol as wsframeproto


RECEIVE_BYTES = 4096
logger = logging.getLogger('trio-websocket')


class ConnectionClosed(Exception):
    '''
    A WebSocket operation cannot be completed because the connection is closed
    or in the process of closing.
    '''
    def __init__(self, reason):
        '''
        Constructor.

        :param CloseReason reason:
        '''
        self.reason = reason

    def __repr__(self):
        ''' Return representation. '''
        return '<{} {}>'.format(self.__class__.__name__, self.reason)


class CloseReason:
    ''' Contains information about why a WebSocket was closed. '''
    def __init__(self, code, reason):
        '''
        Constructor.

        :param int code:
        :param str reason:
        '''
        self._code = code
        try:
            self._name = wsframeproto.CloseReason(code).name
        except ValueError:
            if 1000 <= code <= 2999:
                self._name = 'RFC_RESERVED'
            elif 3000 <= code <= 3999:
                self._name = 'IANA_RESERVED'
            elif 4000 <= code <= 4999:
                self._name = 'PRIVATE_RESERVED'
            else:
                self._name = 'INVALID_CODE'
        self._reason = reason

    @property
    def code(self):
        ''' The numeric close code. '''
        return self._code

    @property
    def name(self):
        ''' The human-readable close code. '''
        return self._name

    @property
    def reason(self):
        ''' An arbitrary reason string. '''
        return self._reason

    def __repr__(self):
        ''' Show close code, name, and reason. '''
        return '<{} code={} name={} reason={}>'.format(self.__class__.__name__,
            self.code, self.name, self.reason)


class WebSocketConnection:
    ''' A WebSocket connection. '''

    CONNECTION_ID = itertools.count()

    def __init__(self, stream, wsproto):
        '''
        Constructor.

        :param SocketStream stream:
        :param wsproto: a WSConnection instance
        :param client: a Trio cancel scope (only used by the server)
        '''
        self._closed = trio.Event()
        self._close_reason = None
        self._id = next(self.__class__.CONNECTION_ID)
        self._message_queue = trio.Queue(0)
        self._stream = stream
        self._wsproto = wsproto
        self._bytes_message = b''
        self._str_message = ''
        self._data_pending = trio.Event()
        self._data_sent = trio.Event()
        self._pong_received = trio.Event()
        self._reader_running = True
        self._writer_running = True
        # The client is responsible for initiating the connection, so it has
        # data ready to send immediately.
        if self.is_client:
            self._data_pending.set()

    @property
    def closed(self):
        '''
        If the WebSocket connection is open and usable, this property is None.
        If the WebSocket connection is closed, no further operations are
        permitted and this property contains a ``CloseReason`` object indicating
        why the connection was closed.
        '''
        return self._close_reason

    @property
    def is_client(self):
        ''' Is this a client instance? '''
        return self._wsproto.client

    @property
    def is_server(self):
        ''' Is this a server instance? '''
        return not self._wsproto.client

    async def close(self, code=1000, reason=None):
        '''
        Close the WebSocket connection.

        This sends a closing frame and suspends until the connection is closed.
        After calling this method, any futher operations on this WebSocket (such
        as ``get_message()`` or ``send_message()``) will raise
        ``ConnectionClosed``.

        :param int code:
        :param str reason:
        :raises ConnectionClosed: if connection is already closed
        '''
        if self._close_reason:
            raise ConnectionClosed(self._close_reason)
        self._wsproto.close(code=code, reason=reason)
        self._close_reason = CloseReason(code, reason)
        self._data_pending.set()
        await self._closed.wait()

    async def get_message(self):
        '''
        Return the next WebSocket message.

        Suspends until a message is available. Raises ``ConnectionClosed`` if
        the connection is already closed or closes while waiting for a message.

        :return: str or bytes
        :raises ConnectionClosed: if connection is closed
        '''
        if self._close_reason:
            raise ConnectionClosed(self._close_reason)
        next_ = await self._message_queue.get()
        if isinstance(next_, Exception):
            raise next_
        else:
            return next_

    async def ping(self, payload):
        '''
        Send WebSocket ping and wait for any pong.

        :param payload: str or bytes payloads
        :raises ConnectionClosed: if connection is closed
        '''
        if self._close_reason:
            raise ConnectionClosed(self._close_reason)
        self._wsproto.ping(payload)
        self._data_pending.set()
        await self._pong_received.wait()
        self._pong_received.clear()

    async def send_message(self, message):
        '''
        Send a WebSocket message.

        Raises ``ConnectionClosed`` if the connection is closed..

        :param message: str or bytes
        :raises ConnectionClosed: if connection is closed
        '''
        if self._close_reason:
            raise ConnectionClosed(self._close_reason)
        self._wsproto.send_data(message)
        self._data_pending.set()
        await self._data_sent.wait()
        self._data_sent.clear()

    async def _close_message_queue(self):
        '''
        If any tasks are suspended on get_message(), wake them up with a
        ConnectionClosed exception.
        '''
        exc = ConnectionClosed(self._close_reason)
        logger.debug('conn#%d websocket closed %r', self._id, exc)
        while True:
            try:
                self._message_queue.put_nowait(exc)
                await trio.sleep(0)
            except trio.WouldBlock:
                break

    async def _close_stream(self):
        ''' Close the TCP connection. '''
        self._reader_running = False
        self._writer_running = False
        self._data_pending.set()
        try:
            await self._stream.aclose()
        except trio.BrokenStreamError:
            # This means the TCP connection is already dead.
            pass
        self._closed.set()

    async def _handle_event(self, event):
        '''
        Process one WebSocket event.

        :param event: a wsproto event
        '''
        if isinstance(event, wsevents.ConnectionRequested):
            logger.debug('conn#%d accepting websocket', self._id)
            self._wsproto.accept(event)
            self._data_pending.set()
        elif isinstance(event, wsevents.ConnectionEstablished):
            logger.debug('conn#%d websocket established', self._id)
        elif isinstance(event, wsevents.ConnectionClosed):
            if self._close_reason is None:
                self._close_reason = CloseReason(event.code, event.reason)
            await self._close_message_queue()
            self._writer_running = False
            self._data_pending.set()
        elif isinstance(event, wsevents.BytesReceived):
            logger.debug('conn#%d received binary frame', self._id)
            self._bytes_message += event.data
            if event.message_finished:
                await self._message_queue.put(self._bytes_message)
                self._bytes_message = b''
        elif isinstance(event, wsevents.TextReceived):
            logger.debug('conn#%d received text frame', self._id)
            self._str_message += event.data
            if event.message_finished:
                await self._message_queue.put(self._str_message)
                self._str_message = ''
        elif isinstance(event, wsevents.PingReceived):
            logger.debug('conn#%d ping', self._id)
            # wsproto queues a pong automatically, we just need to send it:
            self._data_pending.set()
        elif isinstance(event, wsevents.PongReceived):
            logger.debug('conn#%d pong %r', self._id, event.payload)
            self._pong_received.set()
        else:
            raise Exception('Unknown websocket event: {!r}'.format(event))

    async def _reader_task(self):
        ''' A background task that reads network data and generates events. '''
        while self._reader_running:
            # Get network data.
            try:
                data = await self._stream.receive_some(RECEIVE_BYTES)
            except trio.ClosedResourceError:
                break
            if len(data) == 0:
                logger.debug('conn#%d received zero bytes (connection closed)',
                    self._id)
                # If TCP closed before WebSocket, then record it as an abnormal
                # closure.
                if not self._wsproto.closed:
                    self._close_creason = CloseReason(
                        wsframeproto.CloseReason.ABNORMAL_CLOSURE,
                        'TCP connection aborted')
                await self._close_stream()
                break
            else:
                logger.debug('conn#%d received %d bytes', self._id, len(data))
                self._wsproto.receive_bytes(data)

            # Process new events.
            for event in self._wsproto.events():
                await self._handle_event(event)
        logger.debug('conn#%d reader task finished', self._id)

    async def _writer_task(self):
        '''
        A background task that writes data to the network.

        The writer task is a good place to initiate teardown of the underlying
        stream, because we can ensure that all pending WebSocket data has been
        sent first.
        '''
        while self._writer_running:
            await self._data_pending.wait()
            data = self._wsproto.bytes_to_send()
            if len(data) > 0:
                logger.debug('conn#%d sending %d bytes', self._id, len(data))
                await self._stream.send_all(data)
            self._data_pending.clear()
            self._data_sent.set()

        # The server is responsible for initiating TCP shutdown.
        #TODO put client timeout here.
        if self.is_server:
            await self._close_stream()
        logger.debug('conn#%d writer task finished', self._id)


class WebSocketServer:
    '''
    WebSocket server.

    The server class listens on a TCP socket. For each incoming connection,
    it creates a ``WebSocketConnection`` instance, starts some background tasks
    (in a new nursery),
    '''

    def __init__(self, handler, ip, port, ssl_context):
        '''
        Constructor.

        :param coroutine handler: the coroutine to call to handle a new
            connection
        :param str ip: the IP address to bind to
        :param int port: the port to bind to
        :param ssl_context: an SSLContext or None for plaintext
        '''
        self._handler = handler
        self._ip = ip or None
        self._port = port
        self._ssl = ssl_context

    async def listen(self):
        ''' Listen for incoming connections. '''
        try:
            logger.info('Listening on http%s://%s:%d',
                '' if self._ssl is None else 's', self._ip, self._port)
            if self._ssl is None:
                await trio.serve_tcp(self._handle_connection, self._port,
                    host=self._ip)
            else:
                await trio.serve_ssl_over_tcp(self._handle_connection,
                    self._port, ssl_context=self._ssl, https_compatible=True,
                    host=self._ip)
        except KeyboardInterrupt:
            logger.info('Received SIGINT... shutting down')

    async def _handle_connection(self, stream):
        ''' Handle an incoming connection. '''
        async with trio.open_nursery() as nursery:
            wsproto = wsconnection.WSConnection(wsconnection.SERVER)
            connection = WebSocketConnection(stream, wsproto)
            nursery.start_soon(connection._reader_task)
            nursery.start_soon(connection._writer_task)
            nursery.start_soon(self._handler, connection)


class WebSocketClient:
    ''' WebSocket client. '''

    def __init__(self, host, port, resource, use_ssl):
        '''
        Constructor.

        :param str host: the host to connect to
        :param int port: the port to connect to
        :param str resource: the resource (i.e. path without leading slash)
        :param use_ssl: a bool or SSLContext
        '''
        self._host = host
        self._port = port
        self._resource = resource
        if use_ssl == True:
            self._ssl = ssl.create_default_context()
        elif use_ssl == False:
            self._ssl = None
        elif isinstance(use_ssl, ssl.SSLContext):
            self._ssl = use_ssl
        else:
            raise TypeError('`use_ssl` argument must be bool or ssl.SSLContext')

    async def connect(self, nursery):
        '''
        Connect to WebSocket server.

        :param nursery: a Trio nursery to run background connection tasks in
        :raises: OSError if connection attempt fails
        '''
        logger.info('Connecting to http%s://%s:%d/%s',
            '' if self._ssl is None else 's', self._host, self._port,
            self._resource)
        if self._ssl is None:
            stream = await trio.open_tcp_stream(self._host, self._port)
        else:
            stream = await trio.open_ssl_over_tcp_stream(self._host,
                self._port, ssl_context=self._ssl, https_compatible=True)
        if self._port in (80, 443):
            host_header = self._host
        else:
            host_header = '{}:{}'.format(self._host, self._port)
        wsproto = wsconnection.WSConnection(wsconnection.CLIENT,
            host=host_header, resource=self._resource)
        connection = WebSocketConnection(stream, wsproto)
        nursery.start_soon(connection._reader_task)
        nursery.start_soon(connection._writer_task)
        return connection
