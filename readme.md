# Market-Place-Inc — TP Final 

**Sistemas Distribuidos · Ciclo 2026**  
Locks distribuidos con Redis, monitoreo con Prometheus y Grafana, CI/CD con GitHub Actions.

---

## Requisitos previos

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) corriendo
- Python 3.11 (para tests locales y load test)

---

## Levantar el sistema

```bash
docker compose up --build
```

| Servicio | URL | Credenciales |
|---|---|---|
| API de inventario / reservas | http://localhost:8001 | — |
| Grafana | http://localhost:3000 | admin / admin |
| Prometheus | http://localhost:9090 | — |

---

## Probar el endpoint /reserve

```bash
# Reservar 1 unidad de Laptop
curl -s -X POST http://localhost:8001/reserve \
  -H "Content-Type: application/json" \
  -d '{"product_id": "id-1", "quantity": 1}' | python3 -m json.tool
```

| Código | Significado |
|---|---|
| `200` | Reserva exitosa |
| `400` | Sin stock |
| `503` | Otro usuario tiene el candado, reintentar |

---

## Correr los tests

```bash
# 1. Levantar Redis (o usar el del docker compose)
docker run -d -p 6379:6379 redis:7-alpine

# 2. Instalar dependencias
pip install -r catalog/requirements.txt -r tests/requirements.txt

# 3. Generar stubs gRPC
cd catalog && python -m grpc_tools.protoc -I . --python_out=. --grpc_python_out=. catalog.proto && cd ..

# 4. Correr
REDIS_HOST=localhost pytest tests/ -v
```

| Test | Escenario | Resultado esperado |
|---|---|---|
| `test_dos_usuarios_mismo_producto` | 2 usuarios, stock = 1 | 1 exitosa, stock final = 0 |
| `test_cincuenta_usuarios_diez_productos` | 50 usuarios, stock = 10 | 10 exitosas, 40 rechazadas |
| `test_redis_no_disponible` | Redis caído | Responde 503 en < 2 segundos |

---

## Dashboard de Grafana

Con `docker compose up` corriendo:

1. Abrí http://localhost:3000 → admin / admin
2. **Dashboards → Market-Place-Inc — Inventario y Reservas**

| Panel | Qué muestra |
|---|---|
| Latencia (p50 / p95) | Cuánto tarda cada reserva |
| Stock actual | Gauge por producto |
| **Overselling** | Debe ser **siempre 0** |
| Exitosas vs rechazadas | Torta de resultados |
| Usuarios simultáneos | Tasa de intentos/segundo |

---

## Load test con Locust

```bash
pip install locust

# Con salida en consola (10 minutos, 50 usuarios)
locust -f locustfile.py --host http://localhost:8001 \
       --users 50 --spawn-rate 5 --run-time 10m --headless

# O con interfaz web en http://localhost:8089
locust -f locustfile.py --host http://localhost:8001
```

Durante el test verificar en Grafana que **Overselling = 0** en todo momento.

---

## CI/CD — GitHub Actions

Se ejecuta automáticamente en cada `git push`. Pasos:

1. Levanta Redis como servicio
2. Instala dependencias y genera stubs gRPC
3. Corre `pytest tests/ -v`
4. Buildea las imágenes Docker

Ver estado en: **github.com/milimurua/MPI-Pruebas → Actions**
