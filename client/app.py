import logging
import uuid
import json
import asyncio
import os
import signal

import sockjs

from aiohttp import web, ClientSession
from stompest.protocol import StompParser, StompFrame

# Enable logging an INFO level so can see requests.

logging.basicConfig(level=logging.INFO)

async def get_backend_info(url):
    url = url + 'ws/info/'

    async with ClientSession() as session:
        async with session.get(url) as response:
            data = await response.read()

    if response.status == 200:
        info = json.loads(data.decode('UTF-8'))

        # We need to fill in some defaults for values if the
        # service doesn't define them as the user interface
        # expects all fields to be populated.

        info.setdefault('center', {"latitude":"0.0","longitude":"0.0"})
        info.setdefault('zoom', 1)
        info.setdefault('maxZoom', 1)
        info.setdefault('type', 'cluster')
        info.setdefault('visible', 'true')
        info.setdefault('scope', 'all')

        return info

# Background task that periodically polls the list of backend services.

backend_details = {}

def broadcast_message(topic, info):
    manager = sockjs.get_manager('clients', app)

    for session in manager.sessions:
        if not session.expired:
            if hasattr(session, 'subscriptions'):
                if topic in session.subscriptions:
                    subscription = session.subscriptions[topic]

                    headers = {}
                    headers['subscription'] = subscription
                    headers['content-type'] = 'application/json'
                    headers['message-id'] = str(uuid.uuid1())

                    body = json.dumps(info).encode('UTF-8')

                    frame = StompFrame(command='MESSAGE',
                            headers=headers, body=body)

                    session.send(bytes(frame).decode('UTF-8'))

async def poll_backends():
    global backend_details

    loop = asyncio.get_event_loop()

    while True:
        details = {}

        # Get the list of backend services.

        try:
            backends = os.environ.get('BACKEND_SERVICES', 'notebook:8080')

            endpoints = []
            for backend in backends.split(','):
                endpoints.append((backend, 'http://%s/' % backend))

        except Exception:
            logging.exception('Could not query backends.')

            # Wait a while and then update list again.

            await asyncio.sleep(15.0)

            continue

        # Query details for each backend service. The end point is
        # combination of service name and port.

        for name, url in endpoints:
            try:
                info = await get_backend_info(url)
            except Exception as e:
                pass
            else:
                # We will get None if lookup of details failed for service.

                if info is None:
                    continue

                # Ignore the backend if it doesn't provide an id field.

                if 'id' not in info:
                    continue

                details[info['id']] = (name, url, info)

        # Work out what services were added or removed since the last time
        # we ran this. Send notifications to the user interface about
        # whether services were added or removed.

        added = set()
        removed = set(backend_details.keys())

        for name in details:
            if name in removed:
                removed.remove(name)
            if name not in backend_details:
                added.add(name)

        for key in removed:
            name, url, info = backend_details[key]
            broadcast_message('/topic/remove', info)

        for key in added:
            name, url, info = details[key]
            broadcast_message('/topic/add', info)

        # Update our global record of what services we know about.

        backend_details = details

        # Wait a while and then update list again.

        await asyncio.sleep(15.0)

# The aiohttp application.

app = web.Application()

# The websocket endpoint. SockJS is used for basic transport over the
# web socket and then Stomp messaging is used on top. The Stomp module
# only provides message framing, so we need to implemented the
# handshakes ourself which the JS Stomp client is expecting.

def socks_backend(msg, session):
    parser = StompParser('1.1')

    if msg.data:
        parser.add(msg.data.encode('UTF-8'))

    frame = parser.get()

    manager = sockjs.get_manager('clients', app)

    if msg.tp == sockjs.MSG_OPEN:
        pass

    elif msg.tp == sockjs.MSG_MESSAGE:
        if frame.command == 'CONNECT':
            headers = {}
            headers['session'] = session.id

            msg = StompFrame(command='CONNECTED', headers=headers)

            session.send(bytes(msg).decode('UTF-8'))

            session.subscriptions = {}

        elif frame.command == 'SUBSCRIBE':
            subscription = frame.headers['id']
            session.subscriptions[frame.headers['destination']] = subscription

        elif frame.command == 'UNSUBSCRIBE':
            del session.subscriptions[frame.headers['destination']]

    elif msg.tp == sockjs.MSG_CLOSE:
        pass

    elif msg.tp == sockjs.MSG_CLOSED:
        pass

sockjs.add_endpoint(app, socks_backend, name='clients', prefix='/socks-backends/')

# Our REST API endpoints which the web interface uses.

async def backends_list(request):
    details = [info for name, url, info in backend_details.values()]
    return web.json_response(details)

app.router.add_get('/ws/backends/list', backends_list)

async def data_all(request):
    service = request.rel_url.query['service']

    name, url, info = backend_details[service]
    url =  url + 'ws/data/all'

    # XXX Need to find a better way of doing this. It currently reads
    # the whole data set into memory before returning it. Need to work
    # out how can stream the response from backend direct into response
    # to the web interface.

    async with ClientSession() as session:
        async with session.get(url) as response:
            data = await response.read()

    data = json.loads(data.decode('UTF-8'))

    return web.json_response(data, status=response.status)

app.router.add_get('/ws/data/all', data_all)

async def data_within(request):
    service = request.rel_url.query['service']

    name, url, info = backend_details[service]
    url = url + 'ws/data/within'

    # XXX Need to find a better way of doing this. It currently reads
    # the whole data set into memory before returning it. Need to work
    # out how can stream the response from backend direct into response
    # to the web interface.

    async with ClientSession() as session:
        async with session.get(url, params=request.rel_url.query) as response:
            data = await response.read()

    data = json.loads(data.decode('UTF-8'))

    return web.json_response(data, status=response.status)

app.router.add_get('/ws/data/within', data_within)

async def healthz(request):
    return web.json_response('OK')

app.router.add_get('/ws/healthz', healthz)

async def index(request):
    return web.HTTPFound('/index.html')

app.router.add_get('/', index)

app.router.add_static('/', 'static')

# Main application startup.

if __name__ == '__main__':
    loop = asyncio.get_event_loop()

    @asyncio.coroutine 
    def shutdown_application():
        logging.info('Application stopped')
        loop.stop()

    def schedule_shutdown():                          
        logging.info('Stopping application')
        for task in asyncio.Task.all_tasks():
            task.cancel()                    
        asyncio.ensure_future(shutdown_application()) 

    loop.add_signal_handler(signal.SIGTERM, schedule_shutdown)

    # Start up our background task to poll for backend services.

    asyncio.ensure_future(poll_backends(), loop=loop)

    # Run the aiohttpd server.

    web.run_app(app)
