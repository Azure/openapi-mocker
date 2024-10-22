import json
import logging
from flask import request
from datetime import datetime
from resolvers.utils import response_404
from werkzeug.exceptions import BadRequest

logger = logging.getLogger(__name__)

class RequestInterceptor():
    """
    Provides methods for pre-processing HTTP requests before they are resolved and
    post-processing the response before it is sent back to the client.
    """

    @staticmethod
    def before_request():
        """
        Decorator that is executed before any request is processed.
        If it returns a value, this value is used as the response and no resolvers are run.
        """
        # Connexion strict validation causes parameters that are not defined in the API definition to be rejected.
        # Here we strip them out before they are validated:
        #   - Removing '_' parameter as Portal uses this prevent caching.
        #   - Removing filter parameter as the API definition doesn't currently allow for filtering
        operations = [
            RequestInterceptor._log_request,
            RequestInterceptor._redirect_if_number_in_configured_range,
        ]

        # Invoke the operations. If any op returns a value, then use it as the response
        for operation in operations:
            response = operation()
            if response is not None:
                return response


    @staticmethod
    def after_request(response):
        """Decorator that is executed after a request has been processed and just before the reponse is returned"""
        RequestInterceptor._log_response(response)
        return response

    @staticmethod
    def _log_request():
        """Log the incomming request"""
        # get the request information
        path = request.path
        method = request.method
        time = datetime.now().strftime("%H:%M:%S")
        request_body = request.data.decode('utf-8')
        protocol = request.environ.get('SERVER_PROTOCOL')

        # if request_body exists, try to parse the request body as a JSON object and format it with indentation
        # if it fails, log a warning and use the plain text body
        try:
            formatted_body = json.dumps(request.json, indent=2) if request_body else None
        except BadRequest as e:
            logger.warning("Could not parse request body as JSON. Body is not empty")
            formatted_body = request_body

        request_stuff = "[{}] {} {} {}".format(time, method, path, protocol)
        logger.info("Received Request %s:\n---\n%s\n---", request_stuff, formatted_body)


    @staticmethod
    def _log_response(response):
        """Log the generated response"""

        status_code = response.status_code
        headers = dict(response.headers)
        headers_str = json.dumps(headers, indent=2)
        response_body= response.data.decode('utf-8')

        # if response_body exists, try to parse the request body as a JSON object and format it with indentation
        # if it fails, log a warning and use the plain text body
        try:
            formatted_body = json.dumps(response.json, indent=2) if response_body else None
        except BadRequest as e:
            logger.warning("Could not parse response body as JSON. Body is not empty")
            formatted_body = response_body

        if headers:
            logger.info("Generated Response:\nStatus Code: %s\n---\n%s\n---\nHeaders:\n%s\n---", status_code, formatted_body, headers_str)
        else:
            logger.info("Generated Response:\nStatus Code: %s\n---\n%s\n---", status_code, formatted_body)


    def _redirect_if_number_in_configured_range():
        """
        Checks the phone number from the path parameters of the current request and returns
        a preconfigured response if the number falls within certain ranges. If the number
        doesn't fall within these ranges, returns None to indicate that the normal resolver
        should be used.
        """
        # Extract the number from the path parameters
        number = request.view_args.get('phoneNumber') if request.view_args else None
        if number is None:
            return

        # TODO: This is a proof of concept.
        # Check if the number falls within the 404 range
        if number == "+404":
            return response_404(message=f"Forced 404 since number '{number}' lies in range")

        # Check other number ranges
        #...

        return None
