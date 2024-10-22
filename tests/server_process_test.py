import multiprocess
import multiprocessing
import logging
import logging.handlers
import time

import pytest

from openapi_mocker import ServerProcess


@pytest.mark.parametrize(
    "queue",
    [
        multiprocess.Queue,
        multiprocess.SimpleQueue,
        multiprocessing.Queue,
        multiprocessing.SimpleQueue,
    ],
)
@pytest.mark.parametrize(
    "input1, input2, expected_output", [(10, 2, 12), ("Hello", "World", "HelloWorld")]
)
def test_server_process(queue, input1, input2, expected_output):

    input_queue = queue()
    output_queue = queue()
    log_queue = multiprocess.Queue()

    def simple_server(increment):
        try:
            logger = logging.getLogger("simple_server")
            logger.addHandler(logging.handlers.QueueHandler(log_queue))

            logger.info("Server running")
            my_input = input_queue.get()
            logger.info("Received value: %d", my_input)
            output_queue.put(my_input + increment)
            logger.info("Put output value to queue")
            time.sleep(1)
        except Exception():
            logger.exception("An exception occurred")
            return "FAILED"

    sp = ServerProcess(simple_server, args=(input2,), log_queue=log_queue)
    with sp:
        input_queue.put(input1)
        assert output_queue.get() == expected_output
