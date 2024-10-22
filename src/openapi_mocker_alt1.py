"""
A mocking library to generate a live version of an OpenAPI spec.

We've also customised it a very small amount, namely fixing up from `SimpleQueue` to `Queue` in
order to get it working on Windows.

It may also be formatted differently.
"""
import logging
import re
import tempfile
from typing import Optional, Union

import connexion
import connexion.mock
import connexion.resolver
import requests
from pkg_resources import DistributionNotFound, get_distribution

from resolvers.utils import to_pythonic
from .request_interceptor import RequestInterceptor

logger = logging.getLogger(__name__)

# Try and expose __version__, as per PEP396.
try:
    __version__ = get_distribution(__name__).version
except DistributionNotFound:
    # package is not installed
    pass


def ensure_api_is_stored_locally(api_resource):
    """
    Download a remote API to a named temporary file.

    If the API is a remote URL, download and store it in a temporary file.
    If the API is already a local file or a dictionary, it is returned as is.

    :param specification: swagger file with the specification | specification dict
    :type specification: str or dict
    """
    if isinstance(api_resource, dict):
        return api_resource

    if re.match("http(s)?://", api_resource):
        tfile = tempfile.NamedTemporaryFile(delete=False)
        tfile.write(requests.get(api_resource, timeout=5).content)
        tfile.flush()
        return tfile.name
    return api_resource


class OpenApiMocker:
    """Mock microservice providing customisable responses to an OpenAPI/Swagger API."""

    def __init__(
        self,
        api_resource: Union[Optional[str], dict] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        initialise_only: bool = False,
        **kwargs
    ):
        """Wrapper around a Connexion app."""
        logger.debug("Setting up OpenApiMocker for OpenAPI spec %s", api_resource)

        # Disable the request body validation by overriding its value in the default validator map
        # We need to do this because request body validation is not working for required read-only fields
        # TODO: Ideally we need to find a way to enable request body validation for all fields except the read-only ones
        VALIDATOR_MAP = {
            "body": {}
        }

        options = connexion.options.SwaggerUIOptions(swagger_ui=False)
        self.connexion_app = connexion.App(__name__, swagger_ui_options=options, validator_map=VALIDATOR_MAP)

        if api_resource is not None:
            self.connexion_app.add_api(
                ensure_api_is_stored_locally(api_resource),
                resolver=RequestResolver(),
                strict_validation=True,
                validate_responses=True,
                **kwargs,
            )

        self.scheme = "http"
        if host is not None:
            self.host = host
        else:
            self.host = "localhost"
        if port is not None:
            self.port = port
        else:
            self.port = 5000
        self.initialise_only = initialise_only

        def poll():
            """Allow a poll to check that the server is ready."""
            return ("", 204)

        self.connexion_app.add_url_rule("/_openapi_mocker_/poll", "poll", poll)
        self.connexion_app.app.before_request(RequestInterceptor.before_request)
        self.connexion_app.app.after_request(RequestInterceptor.after_request)


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

        logger.debug("Starting OpenApiMocker server")

        self.connexion_app.run(host=self.host, port=self.port, **kwargs)

    @property
    def base_url(self):
        """Return the scheme+host+port at which the app is running."""
        return "{}://{}:{}".format(self.scheme, self.host, self.port)

    def _ping(self):
        requests.get("{}/_openapi_mocker_/poll".format(self.base_url), timeout=1)


class RequestResolver(connexion.resolver.Resolver):
    """Resolve API requests to use customisable responses and store request data."""

    def __init__(self):
        """Initialise as a MockResolver, with callbacks and stored requests."""
        super(RequestResolver, self).__init__()

    def resolve(self, operation):
        """Extend MockResolver to run a succession of callbacks before returning."""
        resolution = super().resolve(operation)
        return connexion.resolver.Resolution(
            self._wrap_resolution(resolution.function, resolution.operation_id),
            resolution.operation_id,
        )

    def resolve_operation_id(self, operation):
        """
        Resolves the operation_id of an operation into a Python function path.

        Example:
            - Accounts_deletePhoneNumbers will be converted to resolvers.accounts.delete_phone_numbers
        """
        PARENT_PATH = "resolvers"

        # Get the associated operation_id from the typespec
        operation_id = super().resolve_operation_id(operation)
        class_name, method_name = RequestResolver._get_class_and_method_names(operation_id)

        # Construct the new operation ID as a python function path
        new_op_id = f"{PARENT_PATH}.{class_name}.{method_name}"
        logger.info("Resolving operation %s to function %s", operation_id, new_op_id)
        return new_op_id

    def _get_class_and_method_names(operation_id):
        """
        Converts operation_id into Pythonic class and method names.

        Example:
            -   Accounts_deletePhoneNumbers will be converted to accounts.delete_phone_numbers
        """
        operation_id_parts = operation_id.split('_')
        class_name = to_pythonic(operation_id_parts[0])
        method_name = to_pythonic('_'.join(operation_id_parts[1:]))

        # If the method_name doesn't exist, set it equal to the class name
        if not method_name or method_name == "":
            method_name = class_name

        return class_name, method_name

    def _wrap_resolution(self, resolver_function, operation_id):
        """Update Resolution to store the request data and run customisations."""

        def wrapper(**kwargs):

            function_path = f"{resolver_function.__module__}.{resolver_function.__name__}"
            logger.info(f"Resolving request with function '{function_path}' and args {kwargs}")

            # Use the resolver function to get the initial response
            response_values = resolver_function(**kwargs)
            headers = None
            if len(response_values) == 3:
                response, status_code, headers = response_values
            else:
                response, status_code = response_values

            if headers is None:
                return response, status_code
            else:
                return response, status_code, headers

        return wrapper
