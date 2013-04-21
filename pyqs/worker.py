#!/usr/bin/env python

import fnmatch
import importlib
from multiprocessing import Event, Process, Queue
from optparse import OptionParser
import os
from Queue import Empty

import boto

from pyqs.utils import decode_message

conn = None


def get_conn():
    # TODO clean this up
    global conn
    if conn:
        return conn
    else:
        conn = boto.connect_sqs()
        return conn


class BaseWorker(Process):
    def __init__(self, *args, **kwargs):
        super(BaseWorker, self).__init__(*args, **kwargs)
        self.should_exit = Event()

    def shutdown(self):
        print "Shutdown initiated"
        self.should_exit.set()


class ReadWorker(BaseWorker):

    def __init__(self, queue, internal_queue, *args, **kwargs):
        super(ReadWorker, self).__init__(*args, **kwargs)
        self.queue = queue
        self.internal_queue = internal_queue

    def run(self):
        print "Running ReadWorker: {}, pid: {}".format(self.queue.name, os.getpid())
        while not self.should_exit.is_set():
            self.read_message()

    def read_message(self):
        messages = self.queue.get_messages(10)
        for message in messages:
            message_body = decode_message(message)
            message.delete()

            self.internal_queue.put(message_body)


class ProcessWorker(BaseWorker):

    def __init__(self, internal_queue, *args, **kwargs):
        super(ProcessWorker, self).__init__(*args, **kwargs)
        self.internal_queue = internal_queue

    def run(self):
        print "Running ProcessWorker, pid: {}".format(os.getpid())
        while not self.should_exit.is_set():
            self.process_message()

    def process_message(self):
        try:
            next_message = self.internal_queue.get(timeout=2)
        except Empty:
            return

        task_path = next_message['task']
        args = next_message['args']
        kwargs = next_message['kwargs']

        task_name = task_path.split(".")[-1]
        task_path = ".".join(task_path.split(".")[:-1])

        task_module = importlib.import_module(task_path)

        task = getattr(task_module, task_name)
        task(*args, **kwargs)


class ManagerWorker(object):

    def __init__(self, queue_prefixes, worker_concurrency):
        self.queue_prefixes = queue_prefixes
        self.queues = self.get_queues_from_queue_prefixes(self.queue_prefixes)
        self.internal_queue = Queue()
        self.reader_children = []
        self.worker_children = []

        for queue in self.queues:
            self.reader_children.append(ReadWorker(queue, self.internal_queue))

        for index in range(worker_concurrency):
            self.worker_children.append(ProcessWorker(self.internal_queue))

    def get_queues_from_queue_prefixes(self, queue_prefixes):
        all_queues = get_conn().get_all_queues()

        matching_queues = []
        for prefix in queue_prefixes:
            matching_queues.extend([
                queue for queue in all_queues if
                fnmatch.fnmatch(queue.name, prefix)
            ])
        return matching_queues

    def start(self):
        for child in self.reader_children:
            child.start()
        for child in self.worker_children:
            child.start()

    def stop(self):
        for child in self.reader_children:
            child.shutdown()
        for child in self.reader_children:
            child.join()

        for child in self.worker_children:
            child.shutdown()
        for child in self.worker_children:
            child.join()


def main():
    parser = OptionParser(usage="usage: pyqs queue_prefix")
    parser.add_option(
        "-c",
        "--concurrency",
        dest="concurrency",
        default=1,
        help="Worker concurrency"
    )
    options, args = parser.parse_args()
    _main(args, **options)


def _main(queue_prefixes, concurrency=5):
    manager = ManagerWorker(queue_prefixes, concurrency)
    manager.start()
