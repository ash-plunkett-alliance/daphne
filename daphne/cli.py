import argparse
import logging
import os
import sys
from argparse import ArgumentError, Namespace
from importlib import import_module

from asgiref.compatibility import guarantee_single_callable

from .access import AccessLogGenerator
from .endpoints import build_endpoint_description_strings
from .server import Server
from .utils import import_by_path

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


class CommandLineInterface:
    """
    Acts as the main CLI entry point for running the server.
    """

    description = "Django HTTP/WebSocket server"

    server_class = Server

    def __init__(self):
        self.parser = argparse.ArgumentParser(description=self.description)
        self.parser.add_argument(
            "-p", "--port", type=int, help="Port number to listen on", default=None
        )
        self.parser.add_argument(
            "-b",
            "--bind",
            dest="host",
            help="The host/address to bind to",
            default=None,
        )
        self.parser.add_argument(
            "--websocket_timeout",
            type=int,
            help="Maximum time to allow a websocket to be connected. -1 for infinite.",
            default=86400,
        )
        self.parser.add_argument(
            "--websocket_connect_timeout",
            type=int,
            help="Maximum time to allow a connection to handshake. -1 for infinite",
            default=5,
        )
        self.parser.add_argument(
            "-u",
            "--unix-socket",
            dest="unix_socket",
            help="Bind to a UNIX socket rather than a TCP host/port",
            default=None,
        )
        self.parser.add_argument(
            "--fd",
            type=int,
            dest="file_descriptor",
            help="Bind to a file descriptor rather than a TCP host/port or named unix socket",
            default=None,
        )
        self.parser.add_argument(
            "-e",
            "--endpoint",
            dest="socket_strings",
            action="append",
            help="Use raw server strings passed directly to twisted",
            default=[],
        )
        self.parser.add_argument(
            "-v",
            "--verbosity",
            type=int,
            help="How verbose to make the output",
            default=1,
        )
        self.parser.add_argument(
            "-t",
            "--http-timeout",
            type=int,
            help="How long to wait for worker before timing out HTTP connections",
            default=None,
        )
        self.parser.add_argument(
            "--access-log",
            help="Where to write the access log (- for stdout, the default for verbosity=1)",
            default=None,
        )
        self.parser.add_argument(
            "--log-fmt",
            help="Log format to use",
            default="%(asctime)-15s %(levelname)-8s %(message)s",
        )
        self.parser.add_argument(
            "--ping-interval",
            type=int,
            help="The number of seconds a WebSocket must be idle before a keepalive ping is sent",
            default=20,
        )
        self.parser.add_argument(
            "--ping-timeout",
            type=int,
            help="The number of seconds before a WebSocket is closed if no response to a keepalive ping",
            default=30,
        )
        self.parser.add_argument(
            "--application-close-timeout",
            type=int,
            help="The number of seconds an ASGI application has to exit after client disconnect before it is killed",
            default=10,
        )
        self.parser.add_argument(
            "--root-path",
            dest="root_path",
            help="The setting for the ASGI root_path variable",
            default="",
        )
        self.parser.add_argument(
            "--proxy-headers",
            dest="proxy_headers",
            help="Enable parsing and using of X-Forwarded-For and X-Forwarded-Port headers and using that as the "
            "client address",
            default=False,
            action="store_true",
        )
        self.arg_proxy_host = self.parser.add_argument(
            "--proxy-headers-host",
            dest="proxy_headers_host",
            help="Specify which header will be used for getting the host "
            "part. Can be omitted, requires --proxy-headers to be specified "
            'when passed. "X-Real-IP" (when passed by your webserver) is a '
            "good candidate for this.",
            default=False,
            action="store",
        )
        self.arg_proxy_port = self.parser.add_argument(
            "--proxy-headers-port",
            dest="proxy_headers_port",
            help="Specify which header will be used for getting the port "
            "part. Can be omitted, requires --proxy-headers to be specified "
            "when passed.",
            default=False,
            action="store",
        )
        self.parser.add_argument(
            "application",
            help="The application to dispatch to as path.to.module:instance.path",
        )
        self.parser.add_argument(
            "-s",
            "--server-name",
            dest="server_name",
            help="specify which value should be passed to response header Server attribute",
            default="daphne",
        )
        self.parser.add_argument(
            "--no-server-name", dest="server_name", action="store_const", const=""
        )
        self.parser.add_argument(
            "--callback-module",
            dest="callback_module",
            help="Specify a module to be loaded and executed during certain stages of initialisation",
        )
        self.parser.add_argument(
            "--chdir",
            dest="chdir",
            help="Specify a directory to change working directory to, eg 'django-site' to run `cd django-site`",
        )

        self.server = None

    @classmethod
    def entrypoint(cls):
        """
        Main entrypoint for external starts.
        """
        cls().run(sys.argv[1:])

    def _check_proxy_headers_passed(self, argument: str, args: Namespace):
        """Raise if the `--proxy-headers` weren't specified."""
        if args.proxy_headers:
            return
        raise ArgumentError(
            argument=argument,
            message="--proxy-headers has to be passed for this parameter.",
        )

    def _get_forwarded_host(self, args: Namespace):
        """
        Return the default host header from which the remote hostname/ip
        will be extracted.
        """
        if args.proxy_headers_host:
            self._check_proxy_headers_passed(argument=self.arg_proxy_host, args=args)
            return args.proxy_headers_host
        if args.proxy_headers:
            return "X-Forwarded-For"

    def _get_forwarded_port(self, args: Namespace):
        """
        Return the default host header from which the remote hostname/ip
        will be extracted.
        """
        if args.proxy_headers_port:
            self._check_proxy_headers_passed(argument=self.arg_proxy_port, args=args)
            return args.proxy_headers_port
        if args.proxy_headers:
            return "X-Forwarded-Port"

    def run(self, args):
        """
        Pass in raw argument list and it will decode them
        and run the server.
        """
        # Decode args
        args = self.parser.parse_args(args)
        # Set up logging
        logging.basicConfig(
            level={
                0: logging.WARN,
                1: logging.INFO,
                2: logging.DEBUG,
                3: logging.DEBUG,  # Also turns on asyncio debug
            }[args.verbosity],
            format=args.log_fmt,
        )
        # If verbosity is 1 or greater, or they told us explicitly, set up access log
        access_log_stream = None
        if args.access_log:
            if args.access_log == "-":
                access_log_stream = sys.stdout
            else:
                access_log_stream = open(args.access_log, "a", 1)
        elif args.verbosity >= 1:
            access_log_stream = sys.stdout

        # change directory, if one is provided
        # (default directory on Heroku is /app/, but we want to run within /app/django-root/)
        if args.chdir:
            print(f"Changing directory to {args.chdir}")
            os.chdir(args.chdir)
        print(f"Current working directory: {os.getcwd()}")

        # Import callback module
        callback_module = None
        if args.callback_module is not None:
            # Let any ModuleNotFound error be raised
            print(f"Looking for module: {args.callback_module}")
            callback_module = import_module(args.callback_module)
        else:
            print("Not looking for module")

        application_path = args.application
        # Run when_init() if method found in callback module
        # Useful to do steps prior to any initialisation happening - eg changing directory, like gunicorn allows
        # https://github.com/benoitc/gunicorn/blob/master/gunicorn/app/base.py#L84-L87
        init_callable = getattr(callback_module, "when_init", None)
        if init_callable:
            init_callable()

        # Import application
        sys.path.insert(0, ".")
        application = import_by_path(application_path)
        application = guarantee_single_callable(application)

        # Set up port/host bindings
        if not any(
            [
                args.host,
                args.port is not None,
                args.unix_socket,
                args.file_descriptor is not None,
                args.socket_strings,
            ]
        ):
            # no advanced binding options passed, patch in defaults
            args.host = DEFAULT_HOST
            args.port = DEFAULT_PORT
        elif args.host and args.port is None:
            args.port = DEFAULT_PORT
        elif args.port is not None and not args.host:
            args.host = DEFAULT_HOST
        # Build endpoint description strings from (optional) cli arguments
        endpoints = build_endpoint_description_strings(
            host=args.host,
            port=args.port,
            unix_socket=args.unix_socket,
            file_descriptor=args.file_descriptor,
        )
        endpoints = sorted(args.socket_strings + endpoints)
        # Start the server
        logger.info("Starting server at {}".format(", ".join(endpoints)))

        # Grab when_ready() callback, if provided
        ready_callable = getattr(callback_module, "when_ready", None)

        self.server = self.server_class(
            application=application,
            endpoints=endpoints,
            http_timeout=args.http_timeout,
            ping_interval=args.ping_interval,
            ping_timeout=args.ping_timeout,
            websocket_timeout=args.websocket_timeout,
            websocket_connect_timeout=args.websocket_connect_timeout,
            websocket_handshake_timeout=args.websocket_connect_timeout,
            application_close_timeout=args.application_close_timeout,
            action_logger=AccessLogGenerator(access_log_stream)
            if access_log_stream
            else None,
            root_path=args.root_path,
            verbosity=args.verbosity,
            proxy_forwarded_address_header=self._get_forwarded_host(args=args),
            proxy_forwarded_port_header=self._get_forwarded_port(args=args),
            proxy_forwarded_proto_header="X-Forwarded-Proto"
            if args.proxy_headers
            else None,
            server_name=args.server_name,
            ready_callable=ready_callable,
        )
        self.server.run()
