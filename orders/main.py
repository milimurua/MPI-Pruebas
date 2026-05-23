import json
import os
import uuid

import grpc
import pika
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import catalog_pb2
import catalog_pb2_grpc


app = FastAPI(title="Orders Service")

CATALOG_GRPC_ADDR = os.getenv("CATALOG_GRPC_ADDR", "catalog:50051")
RABBIT_URL = os.getenv("RABBIT_URL", "amqp://guest:guest@rabbitmq:5672/")
QUEUE_NAME = "order_notifications"


class OrderRequest(BaseModel):
    sku: str
    quantity: int


@app.get("/health")
def health():
    return {"status": "ok", "service": "orders"}


def check_stock(sku, quantity):
    try:
        channel = grpc.insecure_channel(CATALOG_GRPC_ADDR)
        stub = catalog_pb2_grpc.CatalogStub(channel)

        return stub.CheckStock(
            catalog_pb2.StockRequest(sku=sku, quantity=quantity),
            timeout=3,
        )

    except grpc.RpcError as e:
        print(f"Error occurred while checking stock: {e}")
        raise HTTPException(status_code=503, detail="Catalog service unavailable")


def publish_order_event(order_id, sku, quantity):
    try:
        connection = pika.BlockingConnection(pika.URLParameters(RABBIT_URL))
        channel = connection.channel()

        channel.queue_declare(queue=QUEUE_NAME, durable=True)

        message = {
            "message_id": order_id,
            "event_type": "order.created",
            "order_id": order_id,
            "sku": sku,
            "quantity": quantity,
        }

        channel.basic_publish(
            exchange="",
            routing_key=QUEUE_NAME,
            body=json.dumps(message),
            properties=pika.BasicProperties(delivery_mode=2),
        )

        connection.close()

    except Exception:
        raise HTTPException(status_code=503, detail="Could not publish message")


@app.post("/orders", status_code=201)
def create_order(order: OrderRequest):
    if order.quantity <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be greater than zero")

    stock = check_stock(order.sku, order.quantity)

    if not stock.available:
        raise HTTPException(status_code=400, detail="Not enough stock")

    order_id = "ORD-" + uuid.uuid4().hex[:8]

    publish_order_event(order_id, order.sku, order.quantity)

    return {
        "order_id": order_id,
        "sku": order.sku,
        "quantity": order.quantity,
        "product_name": stock.product_name,
        "status": "CREATED",
        "total_price": stock.price * order.quantity,
    }