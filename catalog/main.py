from concurrent import futures
from contextlib import asynccontextmanager
import logging
import os
import threading
import time

import grpc
import redis as redis_lib
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel

import catalog_pb2
import catalog_pb2_grpc


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Redis connection
# ---------------------------------------------------------------------------
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_connect_timeout=3)


# ---------------------------------------------------------------------------
# Catalogo de productos (fuente de verdad para nombres y precios)
# ---------------------------------------------------------------------------
PRODUCTS = {
    "id-1": {"name": "Laptop",   "stock": 10, "price": 1500.0},
    "id-2": {"name": "Mouse",    "stock": 50, "price": 25.0},
    "id-3": {"name": "Keyboard", "stock": 20, "price": 45.0},
    "id-4": {"name": "Monitor",  "stock": 5,  "price": 300.0},
}

LOCK_TTL_SECONDS = 5   # El candado expira solo a los 5 s para evitar deadlocks


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
reserve_attempts = Counter(
    "reserve_attempts_total",
    "Total de intentos de reserva",
    ["status"],   # success | no_stock | locked
)

reserve_duration = Histogram(
    "reserve_duration_seconds",
    "Duracion de cada intento de reserva en segundos",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)

stock_level = Gauge(
    "inventory_stock_level",
    "Stock actual de cada producto",
    ["product_id"],
)

overselling_attempts = Counter(
    "overselling_attempts_total",
    "Intentos de overselling detectados (debe ser SIEMPRE 0)",
)


def _sync_stock_gauges():
    """Actualiza los gauges de Prometheus con el stock actual en Redis."""
    for product_id in PRODUCTS:
        raw = r.get(f"stock:{product_id}")
        current = int(raw) if raw is not None else 0
        stock_level.labels(product_id=product_id).set(current)


# ---------------------------------------------------------------------------
# gRPC service
# ---------------------------------------------------------------------------
class CatalogGrpcService(catalog_pb2_grpc.CatalogServicer):
    def CheckStock(self, request, context):
        logger.info(f"CheckStock gRPC -> sku={request.sku} qty={request.quantity}")
        product = PRODUCTS.get(request.sku)

        if product is None:
            return catalog_pb2.StockResponse(
                sku=request.sku, product_name="", stock=0,
                price=0.0, available=False,
            )

        # Leer stock desde Redis (fuente de verdad compartida)
        raw = r.get(f"stock:{request.sku}")
        current_stock = int(raw) if raw is not None else 0

        return catalog_pb2.StockResponse(
            sku=request.sku,
            product_name=product["name"],
            stock=current_stock,
            price=product["price"],
            available=current_stock >= request.quantity,
        )


def _run_grpc_server():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    catalog_pb2_grpc.add_CatalogServicer_to_server(CatalogGrpcService(), server)
    server.add_insecure_port("[::]:50051")
    server.start()
    logger.info("Catalog gRPC server running on port 50051")
    server.wait_for_termination()


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Inicializar stock en Redis si no existe todavia
    for product_id, data in PRODUCTS.items():
        key = f"stock:{product_id}"
        if r.get(key) is None:
            r.set(key, data["stock"])
            logger.info(f"Stock inicializado en Redis: {key} = {data['stock']}")

    # Inicializar gauges de Prometheus
    _sync_stock_gauges()

    # Arrancar gRPC en hilo daemon
    thread = threading.Thread(target=_run_grpc_server, daemon=True)
    thread.start()

    yield
    # Shutdown: el hilo daemon termina con el proceso


app = FastAPI(title="Catalog / Inventory Service", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------
class ReserveRequest(BaseModel):
    product_id: str
    quantity: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "catalog"}


@app.get("/products/{sku}")
def get_product(sku: str):
    product = PRODUCTS.get(sku)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    raw = r.get(f"stock:{sku}")
    current_stock = int(raw) if raw is not None else 0

    return {
        "sku": sku,
        "name": product["name"],
        "stock": current_stock,
        "price": product["price"],
        "available": current_stock > 0,
    }


@app.post("/reserve")
def reserve(req: ReserveRequest):
    """
    Reserva stock de un producto usando un candado distribuido en Redis.

    Flujo:
      1. Intenta adquirir el lock con SET NX EX (operacion atomica).
      2. Si no puede -> 503 (otro usuario tiene el candado, reintentar).
      3. Lee el stock actual desde Redis.
      4. Si no alcanza -> 400 (sin stock).
      5. Descuenta el stock y lo guarda.
      6. Libera el lock (bloque finally -> siempre se ejecuta).
      7. Devuelve 200 con el stock restante.
    """
    start_time = time.time()

    product_id = req.product_id
    quantity = req.quantity

    if quantity <= 0:
        raise HTTPException(status_code=400, detail="La cantidad debe ser mayor a 0")

    if product_id not in PRODUCTS:
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    lock_key = f"lock:{product_id}"
    stock_key = f"stock:{product_id}"

    # ── 1. Intentar adquirir el candado ──────────────────────────────────────
    # nx=True  -> solo crea la clave si NO existe (operacion atomica en Redis)
    # ex=5     -> el candado expira automaticamente en 5 s (evita deadlocks)
    try:
        acquired = r.set(lock_key, "locked", nx=True, ex=LOCK_TTL_SECONDS)
    except redis_lib.RedisError as e:
        logger.error(f"Redis no disponible: {e}")
        raise HTTPException(status_code=503, detail="Servicio de inventario no disponible")

    if not acquired:
        # Otro usuario tiene el candado en este momento
        reserve_attempts.labels(status="locked").inc()
        reserve_duration.observe(time.time() - start_time)
        raise HTTPException(
            status_code=503,
            detail="Otro usuario esta comprando este producto, reintenta en unos segundos",
        )

    try:
        # ── 2. Leer stock actual ─────────────────────────────────────────────
        raw = r.get(stock_key)
        current_stock = int(raw) if raw is not None else 0

        # ── 3. Verificar si hay suficiente stock ─────────────────────────────
        if current_stock < quantity:
            if current_stock < 0:
                # Esto NO deberia pasar nunca; si pasa, el candado fallo
                overselling_attempts.inc()
                logger.error(f"OVERSELLING DETECTADO! producto={product_id} stock={current_stock}")

            reserve_attempts.labels(status="no_stock").inc()
            reserve_duration.observe(time.time() - start_time)
            raise HTTPException(
                status_code=400,
                detail=f"Sin stock suficiente. Stock actual: {current_stock}",
            )

        # ── 4. Descontar stock ───────────────────────────────────────────────
        new_stock = current_stock - quantity
        r.set(stock_key, new_stock)

        # Actualizar gauge de Prometheus
        stock_level.labels(product_id=product_id).set(new_stock)

        reserve_attempts.labels(status="success").inc()
        reserve_duration.observe(time.time() - start_time)

        logger.info(f"Reserva exitosa: producto={product_id} qty={quantity} stock_restante={new_stock}")
        return {
            "status": "reserved",
            "product_id": product_id,
            "quantity": quantity,
            "stock_remaining": new_stock,
        }

    finally:
        # ── 5. SIEMPRE soltar el candado, incluso si hubo un error ──────────
        r.delete(lock_key)


@app.get("/metrics")
def metrics():
    """Endpoint para Prometheus: expone las metricas en formato text/plain."""
    _sync_stock_gauges()
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
