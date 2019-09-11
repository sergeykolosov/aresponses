import asyncio
import logging
from functools import partial

import pytest
from aiohttp import web, ClientSession
from aiohttp.client_reqrep import ClientRequest
from aiohttp.connector import TCPConnector
from aiohttp.helpers import sentinel
from aiohttp.test_utils import BaseTestServer
from aiohttp.web_response import StreamResponse
from aiohttp.web_runner import ServerRunner
from aiohttp.web_server import Server

from aresponses.utils import _text_matches_pattern, ANY

logger = logging.getLogger(__name__)


class RawResponse(StreamResponse):
    """
    Allow complete control over the response

    Useful for mocking invalid responses
    """

    def __init__(self, body):
        super().__init__()
        self._body = body

    async def _start(self, request, *_, **__):
        self._req = request
        self._keep_alive = False
        writer = self._payload_writer = request._payload_writer
        return writer

    async def write_eof(self, *_, **__):
        await super().write_eof(self._body)


class ResponsesMockServer(BaseTestServer):
    ANY = ANY
    Response = web.Response
    RawResponse = RawResponse

    passthrough_headers = ("content-type",)

    def __init__(self, *, scheme=sentinel, host="127.0.0.1", **kwargs):
        self._responses = []
        self._host_patterns = set()
        self._exception = None
        super().__init__(scheme=scheme, host=host, **kwargs)

    async def _make_runner(self, debug=True, **kwargs):
        srv = Server(self._handler, loop=self._loop, debug=True, **kwargs)
        return ServerRunner(srv, debug=debug, **kwargs)

    async def _close_hook(self):
        return

    async def _handler(self, request):
        return await self._find_response(request)

    def add(self, host, path=ANY, method=ANY, response="", match_querystring=False):
        if isinstance(host, str):
            host = host.lower()

        if isinstance(method, str):
            method = method.lower()

        self._host_patterns.add(host)
        self._responses.append((host, path, method, response, match_querystring))

    def _host_matches(self, match_host):
        match_host = match_host.lower()
        for host_pattern in self._host_patterns:
            if _text_matches_pattern(host_pattern, match_host):
                return True

        return False

    async def _find_response(self, request):
        host, path, path_qs, method = request.host, request.path, request.path_qs, request.method
        logger.info(f"Looking for match for {host} {path} {method}")
        i = 0
        host_matched = False
        path_matched = False
        for host_pattern, path_pattern, method_pattern, response, match_querystring in self._responses:
            if _text_matches_pattern(host_pattern, host):
                host_matched = True
                if (not match_querystring and _text_matches_pattern(path_pattern, path)) or (
                    match_querystring and _text_matches_pattern(path_pattern, path_qs)
                ):
                    path_matched = True
                    if _text_matches_pattern(method_pattern, method.lower()):
                        del self._responses[i]

                        if callable(response):
                            if asyncio.iscoroutinefunction(response):
                                return await response(request)
                            return response(request)

                        if isinstance(response, str):
                            return self.Response(body=response)

                        return response
            i += 1
        self._exception = Exception(f"No Match found for {host} {path} {method}.  Host Match: {host_matched}  Path Match: {path_matched}")
        self._loop.stop()
        raise self._exception  # noqa

    async def passthrough(self, request):
        """Make non-mocked network request"""
        connector = TCPConnector()
        connector._resolve_host = partial(self._old_resolver_mock, connector)

        new_is_ssl = ClientRequest.is_ssl
        ClientRequest.is_ssl = self._old_is_ssl
        try:
            original_request = request.clone(scheme="https" if request.headers["AResponsesIsSSL"] else "http")

            headers = {k: v for k, v in request.headers.items() if k != "AResponsesIsSSL"}

            async with ClientSession(connector=connector) as session:
                async with getattr(session, request.method.lower())(original_request.url, headers=headers, data=(await request.read())) as r:
                    headers = {k: v for k, v in r.headers.items() if k.lower() in self.passthrough_headers}
                    text = await r.text()
                    response = self.Response(text=text, status=r.status, headers=headers)
                    return response
        finally:
            ClientRequest.is_ssl = new_is_ssl

    async def __aenter__(self):
        await self.start_server(loop=self._loop)

        self._old_resolver_mock = TCPConnector._resolve_host

        async def _resolver_mock(_self, host, port, traces=None):
            return [{"hostname": host, "host": "127.0.0.1", "port": self.port, "family": _self._family, "proto": 0, "flags": 0}]

        TCPConnector._resolve_host = _resolver_mock

        self._old_is_ssl = ClientRequest.is_ssl

        def new_is_ssl(_self):
            return False

        ClientRequest.is_ssl = new_is_ssl

        # store whether a request was an SSL request in the `AResponsesIsSSL` header
        self._old_init = ClientRequest.__init__

        def new_init(_self, *largs, **kwargs):
            self._old_init(_self, *largs, **kwargs)

            is_ssl = "1" if self._old_is_ssl(_self) else ""
            _self.update_headers({**_self.headers, "AResponsesIsSSL": is_ssl})

        ClientRequest.__init__ = new_init

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        TCPConnector._resolve_host = self._old_resolver_mock
        ClientRequest.is_ssl = self._old_is_ssl

        await self.close()
        if self._exception:
            pytest.fail(str(self._exception))
            raise self._exception  # noqa


@pytest.fixture
async def aresponses(event_loop):
    async with ResponsesMockServer(loop=event_loop) as server:
        yield server
