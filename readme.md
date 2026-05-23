# Market-Place-Inc — TP2

**Sistemas Distribuidos · Ciclo 2026**  
Microservicios con Docker, Kubernetes, gRPC y RabbitMQ.

---

## Descripción

El sistema implementa tres microservicios reales del ecosistema Market-Place-Inc que demuestran los 4 hitos del TP2:

| Hito | Tecnología | Qué demuestra |
|------|-----------|---------------|
| 1 — Contenedor | Docker + Compose | Imagen fija, usuario no-root, HEALTHCHECK, multi-servicio |
| 2 — Orquestación | Kubernetes | Deployment, Service, probes, auto-healing |
| 3 — Comunicación sync | gRPC + Protobuf | Contrato tipado, timeout explícito, binario HTTP/2 |
| 4 — Mensajería async | RabbitMQ | Cola durable, ACK manual, idempotencia, persistencia |

---

## Servicios

| Servicio | Puerto (local) | Responsabilidad |
|----------|----------------|-----------------|
| `catalog` | 8001 (REST) · 50051 (gRPC) | Catálogo de productos — gRPC server para verificación de stock |
| `orders` | 8000 (REST) | Orquestador del flujo de compra: REST externo + gRPC client + AMQP publisher |
| `notifications` | — | Consumer AMQP — simula envío de emails de confirmación |
| `rabbitmq` | 15672 (UI) · 5672 (AMQP) | Broker de mensajería |

## Estructura del repositorio

```
mpi-microservice/
├── catalog/
│   ├── Dockerfile          # python:3.11-slim, no-root, HEALTHCHECK
│   ├── main.py             # FastAPI + gRPC server (hilo de fondo)
│   └── requirements.txt
├── orders/
│   ├── Dockerfile          # python:3.11-slim, no-root, HEALTHCHECK
│   ├── main.py             # FastAPI + gRPC client + publisher RabbitMQ
│   └── requirements.txt
├── notifications/
│   ├── Dockerfile          # python:3.11-slim, no-root, HEALTHCHECK
│   ├── worker.py           # Consumer AMQP con ACK manual e idempotencia
│   └── requirements.txt
├── proto/
│   └── catalog.proto       # Contrato gRPC (fuente de verdad)
├── k8s/
│   ├── namespace.yaml
│   ├── catalog.yaml        # Deployment(replicas=2) + Service
│   ├── orders.yaml         # Deployment(replicas=2) + Service + initContainers
│   ├── notifications.yaml  # Deployment(replicas=1)
│   └── rabbitmq.yaml       # Deployment + Service
└── docker-compose.yml
```

---

## Ejecutar con Docker Compose

```bash
# Levantar todos los servicios (incluye build)
docker compose up --build

# Verificar que los 4 contenedores están corriendo
docker ps

# Verificar usuario no-root
docker exec catalog whoami   # → appuser

# Ver métricas de recursos
docker stats --no-stream
```

### Probar el flujo completo

```bash
# Crear un pedido válido (id-1 = Laptop, tiene 10 en stock)
curl -s -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"sku": "id-1", "quantity": 2}' | python3 -m json.tool

# Resultado esperado:
# {"order_id": "ORD-xxxxxxxx", "sku": "id-1", "quantity": 2,
#  "product_name": "Laptop", "status": "CREATED", "total_price": 3000.0}

# Probar sin stock suficiente (id-4 = Monitor, solo 5 unidades)
curl -s -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"sku": "id-4", "quantity": 10}' | python3 -m json.tool
# Resultado esperado: 400 "Not enough stock"
```

### Ver la cola en RabbitMQ

Abrir http://localhost:15672 · Usuario: `guest` · Contraseña: `guest`  
Ir a **Queues → order_notifications** para ver los mensajes procesados.

### Demo de persistencia (Estación 4)

```bash
# 1. Parar el consumer
docker stop notifications

# 2. Crear un pedido → el mensaje queda en la cola
curl -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"sku": "id-3", "quantity": 1}'

# 3. Ver en la UI que Messages Ready = 1

# 4. Volver a levantar el consumer → procesa el pendiente
docker start notifications
docker logs notifications -f
```

---

## Desplegar en Kubernetes

> **Requisito**: Docker Desktop con Kubernetes habilitado (Settings → Kubernetes → Enable Kubernetes).

```bash
# 1. Crear el namespace
kubectl apply -f k8s/namespace.yaml

# 2. Desplegar todos los recursos
kubectl apply -f k8s/

# 3. Ver pods corriendo
kubectl get pods -n marketplace
kubectl get svc -n marketplace

# 4. Acceder al servicio de orders
kubectl port-forward svc/orders 8080:8000 -n marketplace
# → POST http://localhost:8080/orders

# 5. Demo de auto-healing
# Terminal 1:
kubectl get pods -n marketplace -w
# Terminal 2:
kubectl delete pod -n marketplace -l app=catalog
# Ver cómo K8s recrea el pod automáticamente
```

---

## Contrato gRPC

```protobuf
// proto/catalog.proto
syntax = "proto3";
package catalog;

message StockRequest {
  string sku = 1;      // Los números de campo son el contrato binario
  int32 quantity = 2;  // NO se pueden cambiar una vez deployado
}

message StockResponse {
  string sku = 1;
  string product_name = 2;
  int32 stock = 3;
  double price = 4;
  bool available = 5;
}

service Catalog {
  rpc CheckStock(StockRequest) returns (StockResponse);
}
```

Para regenerar los stubs:
```bash
python -m grpc_tools.protoc -I proto --python_out=. --grpc_python_out=. proto/catalog.proto
```

---

## Decisiones de diseño

| Flujo | Protocolo | Justificación |
|-------|-----------|---------------|
| Frontend → orders | REST | API pública, compatible con cualquier cliente |
| orders → catalog | gRPC síncrono | Necesitamos saber el stock ANTES de confirmar el pedido. Timeout=3s para fail-fast |
| orders → notifications | RabbitMQ asíncrono | El email no bloquea al usuario. Si el SMTP falla, el pedido ya está confirmado |

**Propiedades del consumer de notificaciones:**
- `durable=True` → la cola sobrevive reinicios del broker
- `delivery_mode=2` → mensajes persistentes en disco
- `auto_ack=False` → ACK manual después de procesar (at-least-once delivery)
- `prefetch_count=1` → procesa un mensaje a la vez
- `processed_messages` set → idempotencia para mensajes duplicados

---

## Comandos de diagnóstico

```bash
# ¿Están corriendo los pods?
kubectl get pods -n marketplace

# ¿Por qué crashea?
kubectl describe pod <nombre> -n marketplace

# Logs del contenedor anterior (crash)
kubectl logs <nombre> --previous -n marketplace

# ¿El Service tiene endpoints?
kubectl get endpoints -n marketplace

# DNS interno funciona?
kubectl exec -it <pod> -n marketplace -- nslookup catalog

# Acceder a la UI de RabbitMQ en K8s
kubectl port-forward svc/rabbitmq 15672:15672 -n marketplace

# Rollback de un deploy
kubectl rollout undo deployment/catalog -n marketplace

# Escalar a 0 para demo de persistencia
kubectl scale deployment notifications --replicas=0 -n marketplace
```

---

## Productos en el catálogo (para testing)

| SKU | Producto | Stock | Precio |
|-----|----------|-------|--------|
| id-1 | Laptop | 10 | $1500.00 |
| id-2 | Mouse | 50 | $25.00 |
| id-3 | Keyboard | 20 | $45.00 |
| id-4 | Monitor | 5 | $300.00 |
