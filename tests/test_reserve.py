"""
Tests para el endpoint POST /reserve del servicio Catalog/Inventory.

Requisitos para correr localmente:
  1. Tener Redis corriendo: docker run -p 6379:6379 redis:7-alpine
  2. Desde la raiz del repo:
       pip install -r catalog/requirements.txt -r tests/requirements.txt
       cd catalog && python -m grpc_tools.protoc -I . --python_out=. --grpc_python_out=. catalog.proto && cd ..
       REDIS_HOST=localhost pytest tests/ -v

En GitHub Actions Redis se levanta automaticamente como servicio (ver ci-cd.yml).
"""

import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import redis as redis_lib

# ---------------------------------------------------------------------------
# Path setup: agregar catalog/ al path para poder importar main.py
# ---------------------------------------------------------------------------
CATALOG_DIR = os.path.join(os.path.dirname(__file__), "..", "catalog")
sys.path.insert(0, CATALOG_DIR)

# ---------------------------------------------------------------------------
# Importar main UNA SOLA VEZ a nivel de modulo.
# Si se recarga con importlib.reload(), Prometheus intenta re-registrar las
# metricas y lanza ValueError: "Duplicated timeseries in CollectorRegistry".
# ---------------------------------------------------------------------------
import catalog_pb2       # noqa: E402
import catalog_pb2_grpc  # noqa: E402

# Parchear _run_grpc_server antes del primer import para que no intente
# bindear el puerto 50051 durante los tests.
with patch("threading.Thread"):
    import main as catalog_main  # noqa: E402

# Redirigir Redis al localhost del runner (en CI se inyecta REDIS_HOST=localhost)
_REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
_REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

catalog_main.r = redis_lib.Redis(
    host=_REDIS_HOST,
    port=_REDIS_PORT,
    socket_connect_timeout=3,
)

from fastapi.testclient import TestClient  # noqa: E402

# Cliente HTTP reutilizado en todos los tests (evita reiniciar el lifespan)
_client = TestClient(catalog_main.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Fixture: limpiar Redis antes de cada test
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def reset_redis():
    """Resetea el stock en Redis antes de cada test."""
    r = catalog_main.r
    for pid in catalog_main.PRODUCTS:
        r.delete(f"stock:{pid}")
        r.delete(f"lock:{pid}")
    for pid, data in catalog_main.PRODUCTS.items():
        r.set(f"stock:{pid}", data["stock"])
    yield r


# ---------------------------------------------------------------------------
# Test 1: Dos usuarios compran el mismo producto al mismo tiempo
# ---------------------------------------------------------------------------
def test_dos_usuarios_mismo_producto(reset_redis):
    """
    Escenario: stock = 1, dos usuarios intentan comprar al mismo tiempo.
    Resultado esperado:
      - Exactamente 1 compra exitosa (status 200).
      - El otro recibe 503 (candado tomado) o 400 (sin stock).
      - Stock final = 0  (NUNCA -1, eso seria overselling).
    """
    r = reset_redis
    r.set("stock:id-1", 1)

    results = []
    errors = []

    def comprar():
        try:
            resp = _client.post("/reserve", json={"product_id": "id-1", "quantity": 1})
            results.append(resp.status_code)
        except Exception as e:
            errors.append(str(e))

    t1 = threading.Thread(target=comprar)
    t2 = threading.Thread(target=comprar)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"Errores inesperados: {errors}"

    exitosas = results.count(200)
    assert exitosas == 1, (
        f"Se esperaba 1 compra exitosa, hubo {exitosas}. Resultados: {results}"
    )

    stock_final = int(r.get("stock:id-1"))
    assert stock_final >= 0, f"OVERSELLING DETECTADO: stock es {stock_final} (negativo)"
    assert stock_final == 0, f"Stock final esperado 0, obtenido {stock_final}"


# ---------------------------------------------------------------------------
# Test 2: 50 usuarios, 10 productos — solo 10 compras deben pasar
# ---------------------------------------------------------------------------
def test_cincuenta_usuarios_diez_productos(reset_redis):
    """
    Escenario: stock = 10, 50 usuarios intentan comprar al mismo tiempo.
    Resultado esperado:
      - Exactamente 10 compras exitosas.
      - Los otros 40 reciben 503 o 400.
      - Stock final = 0. Overselling = 0.
    """
    r = reset_redis
    STOCK_INICIAL = 10
    USUARIOS = 50
    r.set("stock:id-2", STOCK_INICIAL)

    results = []
    lock = threading.Lock()

    def comprar():
        resp = _client.post("/reserve", json={"product_id": "id-2", "quantity": 1})
        with lock:
            results.append(resp.status_code)

    threads = [threading.Thread(target=comprar) for _ in range(USUARIOS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    exitosas = results.count(200)
    rechazadas = len([c for c in results if c in (400, 503)])

    assert exitosas == STOCK_INICIAL, (
        f"Se esperaban {STOCK_INICIAL} compras exitosas, hubo {exitosas}. "
        f"Codigos: {dict((c, results.count(c)) for c in set(results))}"
    )
    assert rechazadas == USUARIOS - STOCK_INICIAL, (
        f"Se esperaban {USUARIOS - STOCK_INICIAL} rechazos, hubo {rechazadas}"
    )

    stock_final = int(r.get("stock:id-2"))
    assert stock_final >= 0, f"OVERSELLING DETECTADO: stock es {stock_final} (negativo)"
    assert stock_final == 0, f"Stock final esperado 0, obtenido {stock_final}"


# ---------------------------------------------------------------------------
# Test 3: Cuando Redis esta caido — el sistema responde rapido con 503
# ---------------------------------------------------------------------------
def test_redis_no_disponible(reset_redis):
    """
    Escenario: Redis no esta disponible (simulado con mock).
    Resultado esperado:
      - El sistema devuelve 503 rapidamente.
      - NO se cuelga esperando indefinidamente.
    """
    redis_roto = MagicMock()
    redis_roto.set.side_effect = redis_lib.RedisError("Connection refused")

    original_r = catalog_main.r
    catalog_main.r = redis_roto

    try:
        start = time.time()
        resp = _client.post("/reserve", json={"product_id": "id-1", "quantity": 1})
        elapsed = time.time() - start

        assert resp.status_code == 503, f"Se esperaba 503, obtuvo {resp.status_code}"
        assert elapsed < 2.0, f"El sistema tardo demasiado: {elapsed:.2f}s (esperado < 2s)"
    finally:
        catalog_main.r = original_r
