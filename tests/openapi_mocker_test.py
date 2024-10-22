import pytest
import requests

from openapi_mocker import OpenApiMocker

SIMPLE_SWAGGER = """swagger: "2.0"
info:
  version: "1.0.0"
  title: minimal
  description: Example of the bare minimum Swagger spec
paths:
  /users:
    get:
      operationId: "users"
      responses:
        200:
          description: Describe the 200 response in more detail
          schema:
            type: array
            items:
              type: string
              example: "User One"
        401:
          description: Not allowed
"""


@pytest.fixture()
def swagger_path(tmpdir):
    swagger_path = tmpdir.join("swagger.yaml")
    swagger_path.write_text(SIMPLE_SWAGGER, "utf-8")
    return str(swagger_path)


def test_server_contextmanager(swagger_path):
    """Minimal test using context manager style."""
    with OpenApiMocker(swagger_path) as api_mock:
        get = requests.get("http://{}:{}/users".format(api_mock.host, api_mock.port))
        assert len(list(api_mock.stored_requests("users"))) == 1
        assert get.status_code == 200


def test_server_start_stop(swagger_path):
    """Minimal test using start() and stop() functions."""
    api_mock = OpenApiMocker(swagger_path)
    api_mock.start()
    get = requests.get("http://{}:{}/users".format(api_mock.host, api_mock.port))
    assert len(list(api_mock.stored_requests("users"))) == 1
    assert get.status_code == 200
    api_mock.stop()


def test_server_double_start(swagger_path):
    """Check that repeated calls to start() are handled smoothly."""
    api_mock = OpenApiMocker(swagger_path)
    api_mock.start()
    api_mock.start()
    api_mock.stop()


def test_server_double_stop(swagger_path):
    """Check that repeated calls to stop() are handled smoothly."""
    api_mock = OpenApiMocker(swagger_path)
    api_mock.start()
    api_mock.stop()
    api_mock.stop()


def test_bad_request(swagger_path):
    """Check that requests to non-existant routes return 404."""
    with OpenApiMocker(swagger_path) as api_mock:
        get = requests.get("http://{}:{}/badpath".format(api_mock.host, api_mock.port))
        assert get.status_code == 404


def test_callback_function(swagger_path):
    """Apply a callback function to customise the response from the mock."""

    def return_202(_default_response, _default_status_code, **req_data):
        """Return a status code of 202 instead."""
        return _default_response, 202

    def extend_response(_default_response, _default_status_code, **req_data):
        """Add a new user to the exampole response."""
        return [*_default_response, "User Two"], _default_status_code

    with OpenApiMocker(swagger_path) as api_mock:
        api_mock.register_callback("users", return_202)
        get = requests.get(f"{api_mock.base_url}/users")
        assert get.status_code == 202
        assert get.json() == ["User One"]

        # Apply a second callback function, both should run
        api_mock.register_callback("users", extend_response)
        get = requests.get(f"{api_mock.base_url}/users")
        assert get.status_code == 202
        assert get.json() == ["User One", "User Two"]


def test_fixed_response(swagger_path):
    """Return a custom fixed response from the mock."""
    with OpenApiMocker(swagger_path) as api_mock:
        api_mock.set_operation_response("users", 401, "Forbidden")
        get = requests.get(f"{api_mock.base_url}/users")
        assert get.status_code == 401
        assert get.json() == "Forbidden"


def test_remote_api():
    api_mock = OpenApiMocker(
        "https://raw.githubusercontent.com/OAI/OpenAPI-Specification/3.0.0/examples/v3.0/api-with-examples.yaml",
    )
    api_mock.start()
    get = requests.get("http://{}:{}/".format(api_mock.host, api_mock.port))
    assert len(get.json()) > 0
    assert get.status_code == 200
    api_mock.stop()
