"""
This module defines the RpcServer class, which listens for and executes RPC requests
sent by the server. It handles reconnection if the connection is lost.
"""

from dataclasses import asdict, dataclass
import json
import threading
import logging
import pika
from typing import Optional
from common.qmsg_model import RpcRequest
from common.qmsg_tools import QMessageTools


@dataclass
class RpcReceiverConfig:
    """
    Configuration dataclass for RpcServer.
    Holds the RabbitMQ connection details and operational parameters.
    """

    rabbitmq_host: str = "localhost"
    reconnect_time: int = 5
    force_name: str = ""
    force_ip: str = ""
    log_topic: str = "RpcServer"


class RpcReceiver:
    """
    The RpcServer class listens for and executes RPC requests sent by the server.
    It handles reconnection if the connection is lost.
    """

    def __init__(
        self,
        owner,
        config: RpcReceiverConfig,
    ) -> None:
        """
        Initialize the RpcServer with the provided RabbitMQ connection details.

        :param rabbitmq_host: The RabbitMQ host address.
        :param queue_name: The queue name for RPC requests.
        :param log_topic: The logging topic for RpcServer.
        """
        self.owner = owner
        self.config = config
        self.tools = QMessageTools()
        self.name = self.config.force_name
        if self.name == "":
            self.name = self.tools.get_own_name()
        self.ip_address = self.config.force_ip
        if self.ip_address == "":
            self.ip_address = self.tools.get_own_ip()

        self.is_handling_request = False
        self.logger = logging.getLogger(self.config.log_topic)
        self.connection: Optional[pika.BlockingConnection] = None
        self.channel: Optional[pika.channel.Channel] = None
        self.thread_listen: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def _close_connection(self) -> None:
        try:
            if self.channel is not None and self.channel.is_open:
                self.channel.close()
        except Exception:
            pass
        try:
            if self.connection is not None and self.connection.is_open:
                self.connection.close()
        except Exception:
            pass
        self.channel = None
        self.connection = None

    def connect(self) -> None:
        """
        Establish a connection to RabbitMQ. If the connection fails, retry after a delay.
        """
        while not self._stop_event.is_set():
            self.logger.info("Connection Loop")
            try:
                self._close_connection()
                self.connection = pika.BlockingConnection(
                    pika.ConnectionParameters(
                        host=self.config.rabbitmq_host,
                        heartbeat=0,
                    )
                )
                if self.connection is not None:
                    self.channel = self.connection.channel()
                    self.channel.queue_declare(queue=self.ip_address)
                    self.logger.info(
                        "RpcReceiver Connected to RabbitMQ at %s",
                        self.config.rabbitmq_host,
                    )
                    break
            except pika.exceptions.AMQPConnectionError as exe:
                self.logger.error(
                    "Connection failed, retrying in 5 seconds... Error: %s",
                    exe,
                )
                self._stop_event.wait(self.config.reconnect_time)

    def on_request(self, channel, method, properties, body) -> None:
        """
        Handle incoming RPC requests. This method processes the request synchronously.
        """
        self.is_handling_request = True
        try:
            raw_dict = json.loads(body.decode())
            request = RpcRequest(**raw_dict)
            response = self.owner.handle_request(request)

            channel.basic_publish(
                exchange="",
                routing_key=properties.reply_to,
                properties=pika.BasicProperties(
                    correlation_id=properties.correlation_id,
                    content_type="application/json",
                ),
                body=json.dumps(asdict(response)),
            )
            channel.basic_ack(delivery_tag=method.delivery_tag)
        finally:
            self.is_handling_request = False

    def run_in_thread(self) -> threading.Thread:
        """
        Run the RPC server in a separate thread.
        """
        self._close_connection()
        self.thread_listen = threading.Thread(target=self.start_listening, daemon=True)
        self.thread_listen.start()
        self.logger.info("RpcServer thread started.")
        return self.thread_listen

    def start_listening(self) -> None:
        """
        Start listening for incoming RPC requests. This function executes the requests.
        """
        self.logger.info(
            "RpcServer is now listening for requests (%s)", self.ip_address
        )
        while not self._stop_event.is_set():
            try:
                if self.connection is None or self.connection.is_closed:
                    self.logger.info("Listen loop CONNECT")
                    self.connect()
                if self.channel is None or self.channel.is_closed:
                    self.logger.info("No channel")
                    self._close_connection()
                    self._stop_event.wait(self.config.reconnect_time)
                    continue

                self.logger.info("Consuming now")
                self.channel.basic_consume(
                    queue=self.ip_address,
                    on_message_callback=self.on_request,
                    auto_ack=False,
                )
                self.channel.start_consuming()
            except Exception as exe:
                if self._stop_event.is_set():
                    break
                self.logger.error("Connection error while consuming. Error: %s", exe)
                self._close_connection()
                self._stop_event.wait(self.config.reconnect_time)

    def stop(self) -> None:
        """
        Stop the RpcServer and its associated thread.
        """
        try:
            self._stop_event.set()
            if self.channel is not None and self.channel.is_open:
                if self.channel.is_consuming:
                    self.channel.stop_consuming()
            self._close_connection()
        except Exception:
            self.logger.info("Exception on stop")
        self.logger.info("RpcServer thread stopped.")
