"""
Load test para el endpoint POST /reserve de Market-Place-Inc.

Simula 50 usuarios comprando al mismo tiempo durante 10 minutos.

Uso:
  pip install locust
  locust -f locustfile.py --host http://localhost:8001 \
         --users 50 --spawn-rate 5 --run-time 10m --headless

O con interfaz web (abre http://localhost:8089):
  locust -f locustfile.py --host http://localhost:8001

Mientras corre, observar el dashboard de Grafana en http://localhost:3000
y verificar que el panel "Overselling" se mantenga siempre en 0.
"""

import random
from locust import HttpUser, between, task


PRODUCTS = ["id-1", "id-2", "id-3", "id-4"]


class CompradorUser(HttpUser):
    """Simula un usuario que intenta comprar productos."""

    # Espera entre 0.5 y 2 segundos entre cada accion (simula comportamiento humano)
    wait_time = between(0.5, 2)

    @task(3)
    def reservar_producto(self):
        """Tarea principal: intentar reservar 1 unidad de un producto aleatorio."""
        product_id = random.choice(PRODUCTS)

        with self.client.post(
            "/reserve",
            json={"product_id": product_id, "quantity": 1},
            catch_response=True,
            name="POST /reserve",
        ) as response:
            if response.status_code == 200:
                # Reserva exitosa
                response.success()
            elif response.status_code in (400, 503):
                # Sin stock o candado ocupado: son respuestas validas del sistema
                # NO son fallos del load test (el sistema funciona correctamente)
                response.success()
            else:
                # Cualquier otro codigo es un error inesperado
                response.failure(f"Codigo inesperado: {response.status_code}")

    @task(1)
    def ver_producto(self):
        """Tarea secundaria: consultar stock de un producto (trafico de lectura)."""
        product_id = random.choice(PRODUCTS)
        self.client.get(f"/products/{product_id}", name="GET /products/{id}")

    @task(1)
    def health_check(self):
        """Verificar que el servicio este vivo."""
        self.client.get("/health", name="GET /health")
