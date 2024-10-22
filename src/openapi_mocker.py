"""A mocking library to generate a live version of an OpenAPI spec."""
import logging
import logging.handlers
import re
import tempfile
import threading
import time
from collections import defaultdict
from queue import Empty
from typing import Any, Callable, Optional, Tuple

import connexion
import connexion.mock
import connexion.resolver
import multiprocess
import requests
from pkg_resources import DistributionNotFound, get_distribution

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def ensure_api_is_stored_locally(api_path):
    """Download a remote API to a named temporary file."""
    if re.match("http(s)?://", api_path):
        tfile = tempfile.NamedTemporaryFile(delete=False)
        tfile.write(requests.get(api_path, timeout=5).content)
        tfile.flush()
        return tfile.name
    return api_path


class ServerProcess:
    def __init__(self, target=None, *, log_queue=None, args=(), kwargs={}):
        self.server_process = None
        self.target = target
        self.args = args
        self.kwargs = kwargs
        self.log_queue = log_queue

        if self.log_queue is not None:
            self.server_log_listener = logging.handlers.QueueListener(
                self.log_queue, respect_handler_level=True
            )
        else:
            self.server_log_listener = None

    def __enter__(self):
        """Start the server as a contextmanager."""
        self.start()
        return self

    def __exit__(self, *args):
        """Stop the server as a context manager."""
        self.stop()

    def start(self, *args, **kwargs):
        """
        Run the Server in another process and wait for it to become active.

        Keyword arguments are passed into the app.run() method.
        """
        if args:
            self.args = args
        self.kwargs = {**self.kwargs, **kwargs}

        if self.is_ready():
            logger.warning("Server is already running!")
            return

        if self.server_log_listener is not None:
            self.server_log_listener.start()

        self.server_process = multiprocess.get_context("fork").Process(
            target=self.target, args=self.args, kwargs=self.kwargs, daemon=True,
        )
        logger.info("Starting Server process")
        self.server_process.start()

        logger.debug("Waiting until server is ready")
        self._wait_until_ready()
        logger.debug("Server has started")

    def is_ready(self):
        if self.server_process is not None:
            return self.server_process.is_alive()
        else:
            return False

    def stop(self):
        """Stop the server process and wait for it to terminate."""
        if self.server_process is None:
            logger.warning("Server is already stopped!")
            return
        logger.debug("Stopping Server process")

        self.server_process.terminate()
        self.server_process.join()

        logger.info("Server has stopped")

        if self.server_log_listener is not None:
            self.server_log_listener.stop()

    def _wait_until_ready(self, delay=0.1):
        while not self.is_ready():
            time.sleep(delay)


class OpenApiMocker:
    """Mock microservice providing customisable responses to an OpenAPI/Swagger API."""

    def __init__(
        self,
        api_path: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        **kwargs,
    ):
        """Wrapper around a Connexion app."""
        logger.debug("Setting up OpenApiMocker for OpenAPI spec %s", api_path)

        self.requests_q = multiprocess.Queue()
        log_q = multiprocess.Queue()
        self.response_mod_q = multiprocess.SimpleQueue()

        self.connexion_app = connexion.App(__name__, options={"swagger_ui": False})

        self.server_log_listener = logging.handlers.QueueListener(
            log_q, respect_handler_level=True
        )

        test_resolver = TestResolver(self.requests_q, self.response_mod_q, log_q)

        if api_path is not None:
            self.connexion_app.add_api(
                ensure_api_is_stored_locally(api_path),
                resolver=test_resolver,
                **kwargs,
            )

        self.server_process = None
        self.scheme = "http"
        if host is not None:
            self.host = host
        else:
            self.host = "localhost"
        if port is not None:
            self.port = port
        else:
            self.port = 5000

        self._stored_requests = []

        def ping():
            """Allow a ping to check that the server is ready."""
            return ("", 204)

        self.connexion_app.add_url_rule(
            "/_openapi_mocker_/ping", "ping", test_resolver.ping
        )

    def __enter__(self):
        """Start the app as a contextmanager."""
        self.start()
        return self

    def __exit__(self, *args):
        """Stop the app as a context manager."""
        self.stop()

    def add_url_rule(self, *args, **kwargs) -> None:
        """
        Add a custom route.

        Directly invokes the add_url_rule() method of the connexion App.
        """
        return self.connexion_app.add_url_rule(*args, **kwargs)

    def start(self, host=None, port=None, **kwargs):
        """
        Run the Connexion app in another process and wait for it to become active.

        Keyword arguments are passed into the app.run() method.
        """
        if host is not None:
            self.host = host
        if port is not None:
            self.port = port

        if self.server_process is not None:
            logger.debug("OpenApiMocker is already running")
            return
        logger.debug("Starting OpenApiMocker server")

        self.server_log_listener.start()

        self.server_process = multiprocess.Process(
            target=self.connexion_app.run,
            kwargs={"host": self.host, "port": self.port, **kwargs},
            daemon=True,
        )
        self.server_process.start()

        self._wait_until_up()
        logger.info(
            "Started OpenApiMocker server process, serving on %s:%s",
            self.host,
            self.port,
        )

    def stop(self):
        """Stop the Connexion app and wait for it to terminate."""
        if self.server_process is None:
            logger.debug("OpenApiMocker is already stopped")
            return
        logger.debug("Stopping OpenApiMocker server.")

        self.server_process.terminate()
        self.server_process = None

        self._wait_until_stopped()
        logger.info("Stopped OpenApiMocker server process")

        self.server_log_listener.stop()

    @property
    def base_url(self):
        """Return the scheme+host+port at which the app is running."""
        return "{}://{}:{}".format(self.scheme, self.host, self.port)

    def _wait_until_up(self):
        while True:
            try:
                self._ping()
            except requests.exceptions.ConnectionError:
                time.sleep(0.1)
            else:
                break

    def _wait_until_stopped(self):
        while True:
            try:
                self._ping()
            except requests.exceptions.ConnectionError:
                break
            else:
                time.sleep(0.1)

    def _ping(self):
        requests.get("{}/_openapi_mocker_/ping".format(self.base_url), timeout=1)

    def register_response_mod(
        self, operation_id: str, response_mod: Callable[[Any, int], Tuple[Any, int]]
    ) -> None:
        """
        Add a response modifier that will be run for the given `operation_id`.

        Respose modifiers should be functions and will be called with keyword
        arguments:

        * `_default_response`: The data to be returned in the response (From the
          example response provided in the API, and customised by previous
          modifiers.)
        * `_default_status_code`: The status code to be returned (The lowest numbered
          code in the API unless customised by previous modifier functions.)
        * Any data in the request as keyword arguments.

        They must return a tuple containing the updated (or unchanged) response and
        status code.
        """
        logger.info("Adding a response modifier for operation ID: %s", operation_id)
        self._put_to_response_mod_q((operation_id, response_mod))

    def set_fixed_response(
        self, operation_id: str, status_code: int, response_body: Any
    ) -> None:
        """
        Override any default response with a fixed response.

        Matches based on operationId in the schema. For more customisable responses
        based on request details, use response modifier functionality directly.
        """

        self.register_response_mod(operation_id, (response_body, status_code))

    def _get_from_requests_q(self):
        """Fetch the latest requests to the server."""
        while True:
            try:
                next_request = self.requests_q.get(block=False)
            except Empty:
                break
            else:
                self._stored_requests.append(next_request)

    def stored_requests(self, operation_id=None):
        """Store data in requests against the operation ID for later verification."""
        self._get_from_requests_q()

        for request in self._stored_requests:
            if operation_id is None or request[0] == operation_id:
                yield request

    def clear_stored_requests(self):
        """Clear stored requests."""
        self._get_from_requests_q()

        logger.debug("Clearing all stored requests")
        self._stored_requests.clear()

    def clear_response_modifiers(self):
        self._put_to_response_mod_q(None)

    def reset(self):
        self.clear_response_modifiers()
        self.clear_stored_requests()

    def _put_to_response_mod_q(self, value):
        logger.debug("Adding to response modifier queue")
        self.response_mod_q.put(value)
        logger.debug("Added to response modifier queue")
        # Invoke the server (if running) to ensure that we pull the latest changes
        if self.server_process is not None:
            # Delay until queue is not empty() or we have waited a second.
            # This is required, because it takes non-zero time of the object to
            # actually appear in the queue.
            put_time = time.time()
            while self.response_mod_q.empty() and time.time() < put_time + 1:
                time.sleep(0.01)

            self._ping()
            logger.debug("Pinged to empty response modifier queue")


class TestResolver(connexion.mock.MockResolver):
    """Resolve API requests to use customisable responses and store request data."""

    def __init__(self, requests_q, response_mod_q, log_q):
        """Initialise as a MockResolver, with callbacks and stored requests."""
        super(TestResolver, self).__init__(mock_all=False)
        self.requests_q = requests_q
        self.response_mod_q = response_mod_q
        self._response_modifiers = defaultdict(list)
        self.response_mod_q_lock = threading.Lock()

        # self.logger = multiprocess.get_logger()
        self.logger = logging.getLogger(f"{__name__}.TestResolver")
        handler = logging.handlers.QueueHandler(log_q)
        self.logger.addHandler(handler)

    def resolve(self, operation):
        """Extend MockResolver to run a succession of callbacks before returning."""
        resolution = super().resolve(operation)
        return connexion.resolver.Resolution(
            self._wrap_resolution(resolution.function, resolution.operation_id),
            resolution.operation_id,
        )

    def ping(self):
        """Return an empty response to confirm that the server is up and ready."""
        self._get_from_response_mod_q()

        return ("", 204)

    def _get_from_response_mod_q(self):
        """
        Fetch the latest response customisations.

        The modifier can be of any of the following forms:

        * A function taking two parameters (unmodified response and
          unmodified status code) and returning a tuple (new response and
          new status code).
        * A tuple of a fixed status code and a function taking no parameters
          that returns a fixed response.
        * A tuple of a fixed status code and a fixed response.

        If None is received, then all customisations are cleared.
        """
        self.logger.debug("Trying to acquire Response modifier Queue lock.")
        self.response_mod_q_lock.acquire(blocking=True, timeout=20)
        while True:
            if not self.response_mod_q.empty():
                self.logger.info("Response modifier Queue is not empty")
                response_mod_item = self.response_mod_q.get()
            else:
                self.logger.debug("Response modifier Queue is empty")
                break

            if response_mod_item is not None:
                operation_id, response_mod = response_mod_item

                if callable(response_mod):
                    self.logger.info(
                        "Adding response modifier for %s - callback function",
                        operation_id,
                    )
                    response_mod_callback = response_mod
                else:
                    if callable(response_mod[0]):
                        self.logger.info(
                            "Adding response modifier for %s - generated fixed response",
                            operation_id,
                        )
                        fixed_response_body = response_mod[0]()
                        fixed_status_code = response_mod[1]
                    else:
                        self.logger.info(
                            "Adding response modifier for %s - fixed response",
                            operation_id,
                        )
                        fixed_response_body = response_mod[0]
                        fixed_status_code = response_mod[1]

                    self.logger.debug(
                        "Fixed response for %s has code %d and length %d",
                        operation_id,
                        fixed_status_code,
                        len(str(fixed_response_body)),
                    )
                    # See https://docs.python.org/3/faq/programming.html#why-do-lambdas-defined-in-a-loop-with-different-values-all-return-the-same-result
                    response_mod_callback = (lambda b, c: (lambda **_: (b, c)))(
                        fixed_response_body, fixed_status_code
                    )

                self._response_modifiers[operation_id].append(response_mod_callback)
            else:
                self.logger.info("Clearing all response modifiers")
                self._response_modifiers.clear()
        self.logger.debug(
            "Response modifier counts: %s",
            {k: len(v) for k, v in self._response_modifiers.items()},
        )
        self.response_mod_q_lock.release()

    def _put_latest_request(self, operation_id, **kwargs):
        self.requests_q.put((operation_id, kwargs))

    def _wrap_resolution(self, default_function, operation_id):
        """Update Resolution to store the request data and run customisations."""

        def wrapper(**kwargs):
            # Save request to queue for later verification
            self._put_latest_request(operation_id, **kwargs)

            # Fetch any latest response modifiers
            self._get_from_response_mod_q()

            # Use the default operation that returns an example response.
            response, status_code = default_function(**kwargs)
            headers = None

            for response_mod in self._response_modifiers[operation_id]:
                new_response_values = response_mod(
                    _default_response=response,
                    _default_status_code=status_code,
                    **kwargs,
                )
                if len(new_response_values) == 3:
                    response, status_code, headers = new_response_values
                else:
                    response, status_code = new_response_values

            if headers is None:
                return response, status_code
            else:
                return response, status_code, headers

        return wrapper

    def mock_operation(self, operation, *args, **kwargs):
        """Generate default response."""
        resp, code = operation.example_response()
        if resp is not None:
            return resp, code
        return "No example response was defined.", code


if __name__ == "__main__":
    import sys, time

    args = sys.argv[1:]
    o = OpenApiMocker(args[0])
    o.start()
    while True:
        time.sleep(10)
