"""
Tests para el endpoint POST /reserve del servicio Catalog/Inventory.

Requisitos para correr localmente:
  1. Tener Redis corriendo: docker run -p 6379:6379 redis:7-alpine
  2. Desde la carpeta mpi-microservice/catalog/:
       pip install -r requirements.txt
       pytest ../tests/test_reserve.py -v

En GitHub Actions Redis se levanta automaticamente como servicio (ver ci-cd.yml).
"""

import os
import sys
import threading
import time
from unittest.mock import patch, MagicMock

import pytest
import redis as redis_lib
from fastapi.testclient import TestClient

CATALOG_DIR = os.path.join(os.path.dirname(__file__), "..", "catalog")
sys.path.insert(0, CATALOG_DIR)

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

# Modulo de la app (importado despues de ajustar el path)
import catalog_pb2          # noqa: E402  (necesario para que el import de main no falle)
import catalog_pb2_grpc     # noqa: E402


@pytest.fixture(autouse=True)
def app_client():
    """
    Crea un TestClient de FastAPI y limpia Redis antes de cada test.
    Parchea el gRPC para que no intente arrancar en los tests unitarios.
    """
    # Parche: evitar que el servidor gRPC arranque realmente durante los tests
    with patch("threading.Thread") as mock_thread:
        mock_thread.return_value = MagicMock()

        # Importar main DESPUES de parchear para que tome el mock
        import importlib
        import main as catalog_main
        importlib.reload(catalog_main)

        # Apuntar Redis al localhost del runner de tests
        catalog_main.r = redis_lib.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            socket_connect_timeout=3,
        )

        # Limpiar stock y locks del test anterior
        r = catalog_main.r
        for pid in catalog_main.PRODUCTS:
            r.delete(f"stock:{pid}")
            r.delete(f"lock:{pid}")

        # Inicializar stock en Redis (simula el lifespan)
        for pid, data in catalog_main.PRODUCTS.items():
            r.set(f"stock:{pid}", data["stock"])

        client = TestClient(catalog_main.app)
        yield client, catalog_main.r


# Test 1
def test_dos_usuarios_mismo_producto(app_client):
    """
    Escenario: stock = 1, dos usuarios intentan comprar al mismo tiempo.
    Resultado esperado:
      - Exactamente 1 compra exitosa (status 200).
      - El otro recibe 503 (candado tomado) o 400 (sin stock).
      - Stock final = 0  (NUNCA -1, eso seria overselling).
    """
    client, r = app_client

    # Fijar stock = 1 para este producto
    r.set("stock:id-1", 1)

    results = []
    errors = []

    def comprar():
        try:
            resp = client.post("/reserve", json={"product_id": "id-1", "quantity": 1})
            results.append(resp.status_code)
        except Exception as e:
            errors.append(str(e))

    # Lanzar 2 hilos concurrentes
    t1 = threading.Thread(target=comprar)
    t2 = threading.Thread(target=comprar)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"Errores inesperados: {errors}"

    # Exactamente una compra exitosa
    exitosas = results.count(200)
    assert exitosas == 1, f"Se esperaba 1 compra exitosa, hubo {exitosas}. Resultados: {results}"

    # Stock final debe ser 0, NUNCA negativo
    stock_final = int(r.get("stock:id-1"))
    assert stock_final == 0, f"Stock final esperado 0, obtenido {stock_final} (posible OVERSELLING)"
    assert stock_final >= 0, "OVERSELLING DETECTADO: stock es negativo"


# Test 2: Escenario con muchos usuarios concurrentes
def test_cincuenta_usuarios_diez_productos(app_client):
    """
    Escenario: stock = 10, 50 usuarios intentan comprar al mismo tiempo.
    Resultado esperado:
      - Exactamente 10 compras exitosas.
      - Los otros 40 reciben 503 o 400.
      - Stock final = 0.
      - Overselling = 0 (nunca -1 ni menos).
    """
    client, r = app_client

    STOCK_INICIAL = 10
    USUARIOS = 50
    r.set("stock:id-2", STOCK_INICIAL)

    results = []
    lock = threading.Lock()

    def comprar():
        resp = client.post("/reserve", json={"product_id": "id-2", "quantity": 1})
        with lock:
            results.append(resp.status_code)

    # Lanzar 50 hilos concurrentes
    threads = [threading.Thread(target=comprar) for _ in range(USUARIOS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    exitosas = results.count(200)
    rechazadas = len([c for c in results if c in (400, 503)])

    # Exactamente 10 compras exitosas
    assert exitosas == STOCK_INICIAL, (
        f"Se esperaban {STOCK_INICIAL} compras exitosas, hubo {exitosas}. "
        f"Total resultados: {dict((c, results.count(c)) for c in set(results))}"
    )

    # Los 40 restantes rechazados
    assert rechazadas == USUARIOS - STOCK_INICIAL, (
        f"Se esperaban {USUARIOS - STOCK_INICIAL} rechazos, hubo {rechazadas}"
    )

    # Stock final = 0, sin overselling
    stock_final = int(r.get("stock:id-2"))
    assert stock_final == 0, f"Stock final esperado 0, obtenido {stock_final}"
    assert stock_final >= 0, "OVERSELLING DETECTADO: stock es negativo"


# Test 3: Cuando Redis esta caido o tarda mucho
def test_redis_no_disponible(app_client):
    """
    Escenario: Redis no esta disponible (simulado con mock).
    Resultado esperado:
      - El sistema responde rapidamente con error 503.
      - NO se cuelga esperando indefinidamente.
    """
    client, r = app_client

    import main as catalog_main

    redis_roto = MagicMock()
    redis_roto.set.side_effect = redis_lib.RedisError("Connection refused")

    original_r = catalog_main.r
    catalog_main.r = redis_roto

    try:
        start = time.time()
        resp = client.post("/reserve", json={"product_id": "id-1", "quantity": 1})
        elapsed = time.time() - start

        # Debe devolver 503
        assert resp.status_code == 503, f"Se esperaba 503, obtuvo {resp.status_code}"

        # Debe responder en menos de 2 segundos
        assert elapsed < 2.0, f"El sistema tardo demasiado: {elapsed:.2f}s (esperado < 2s)"

    finally:
        # Restaurar el cliente Redis original
        catalog_main.r = original_r
