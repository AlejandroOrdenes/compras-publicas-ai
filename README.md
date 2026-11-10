# Compras Públicas AI

Asistente de IA para explorar licitaciones de Mercado Público (ChileCompra) y
consultar la normativa de compras públicas (Ley 19.886 y su reglamento).

> En construcción. Proyecto de aprendizaje y portafolio.

## Levantar en local

```bash
cp .env.example .env          # y completa los valores
docker compose up -d          # PostgreSQL + pgvector
cd backend
python -m venv .venv
source .venv/Scripts/activate # Windows Git Bash  (Linux/Mac: .venv/bin/activate)
pip install -r requirements.txt
uvicorn app.main:app --reload
```
