import json
import os
import time

import pika


RABBIT_URL = os.getenv("RABBIT_URL", "amqp://guest:guest@rabbitmq:5672/")
QUEUE_NAME = "order_notifications"

processed_messages = set()


def connect_to_rabbitmq():
    while True:
        try:
            return pika.BlockingConnection(pika.URLParameters(RABBIT_URL))
        except pika.exceptions.AMQPConnectionError:
            print("RabbitMQ is not ready. Retrying...", flush=True)
            time.sleep(5)


def process_message(message):
    message_id = message["message_id"]

    if message_id in processed_messages:
        print(f"Duplicated message ignored: {message_id}", flush=True)
        return

    print(f"Notification sent for order {message['order_id']}", flush=True)
    processed_messages.add(message_id)


def on_message(channel, method, properties, body):
    try:
        message = json.loads(body)
        process_message(message)

        # Manual ACK: the message is removed only after successful processing.
        channel.basic_ack(delivery_tag=method.delivery_tag)

    except Exception as error:
        print(f"Error processing message: {error}", flush=True)

        # Requeue the message if processing fails.
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def main():
    print("Notifications worker started", flush=True)

    connection = connect_to_rabbitmq()
    channel = connection.channel()

    channel.queue_declare(queue=QUEUE_NAME, durable=True)
    channel.basic_qos(prefetch_count=1)

    channel.basic_consume(
        queue=QUEUE_NAME,
        on_message_callback=on_message,
        auto_ack=False,
    )

    print("Waiting for order notifications...", flush=True)
    channel.start_consuming()


if __name__ == "__main__":
    main()