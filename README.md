# django-cod-settlement

A Django REST API that settles **cash-on-delivery (COD)** money between e-commerce
merchants and the courier companies that collect cash at the customer's door.

Couriers send merchants a periodic **remittance CSV** ("here are the parcels we
delivered, the cash we collected and the fee we kept"). This service ingests those
files, **reconciles every line against the merchant's orders**, flags discrepancies,
computes how much cash each courier still owes, and lets merchants open and export
**disputes**.

Stack: Python 3.12 · Django 5.2 LTS · Django REST Framework · SimpleJWT · django-filter ·
drf-spectacular · Celery + Redis · PostgreSQL · pytest + factory_boy · ruff · uv · Docker.

---

## Features

- **JWT auth** with per-merchant data isolation (a merchant only ever sees its own
  orders, statements and disputes) and a **staff** role that sees everything.
- **Orders & couriers CRUD** with filtering, search, ordering and pagination.
  Order status changes go through a delivery-attempt endpoint so the lifecycle stays valid.
- **CSV remittance upload**: validated synchronously (all row errors reported at once),
  stored, then processed by a **Celery** task. Uploading the same file twice is a
  no-op thanks to a SHA-256 content hash.
- **Reconciliation service** (plain Python, unit-tested without HTTP): matched,
  amount mismatch, unknown tracking number, delivered-but-missing-from-statement.
- **Courier balance**: expected collections vs. reported collections, fees, net remitted.
- **Disputes** on any non-matched result, staff resolution, CSV export.
- **Django admin** with list filters, search and inlines (lines + results on a statement,
  delivery attempts on an order) and a "re-run reconciliation" action.
- **OpenAPI 3** schema and Swagger UI at `/api/docs/`.
- `seed_demo` management command with deterministic demo data and a sample CSV.

## Architecture

```mermaid
flowchart LR
    subgraph Client
        M[Merchant / Ops UI]
    end

    subgraph API["Django + DRF (gunicorn)"]
        V[ViewSets<br/>auth, tenant scoping,<br/>validation]
        S[Service layer<br/>csv_parser · ingestion<br/>reconciliation · balance]
        ADM[Django admin]
    end

    subgraph Worker["Celery worker"]
        T[process_statement task]
    end

    DB[(PostgreSQL)]
    R[(Redis broker)]
    FS[(Media storage<br/>uploaded CSVs)]

    M -- JWT --> V
    V --> S
    V -- "on_commit: delay()" --> R
    R --> T
    T --> S
    S --> DB
    V -- store file --> FS
    T -- read file --> FS
    ADM --> DB
```

```
apps/
  accounts/     Merchant (tenant) + custom User, tenant-scoping permissions
  logistics/    Courier, Order, DeliveryAttempt; orders/couriers API; balance endpoint
  settlement/   RemittanceStatement, RemittanceLine, ReconciliationResult, Dispute
    services/   csv_parser.py, ingestion.py, reconciliation.py, balance.py  <- business rules
    tasks.py    Celery task (thin wrapper over services)
  common/       money.py (Decimal helpers)
config/         settings (django-environ), urls, celery app
tests/          pytest suite
```

Views and the Celery task are deliberately thin: they translate HTTP/queue input into
calls to the `services/` modules, which hold the rules and are tested directly.

## How reconciliation works

A statement is always **one courier × one merchant × one period**.

```mermaid
flowchart TD
    L[CSV line: tracking, collected, fee] --> Q{Order with this tracking<br/>for this courier + merchant?}
    Q -- no --> U[UNKNOWN_TRACKING]
    Q -- yes --> E["expected = amount_due if order is delivered,<br/>else 0 (returned / cancelled parcels collect nothing)"]
    E --> C{collected == expected?}
    C -- yes --> OK[MATCHED]
    C -- no --> MM["AMOUNT_MISMATCH<br/>difference = collected - expected"]
    D[Delivered orders in the period] --> N{On this statement or<br/>already settled on another?}
    N -- neither --> MS[MISSING_FROM_STATEMENT]
```

1. `classify()` is a **pure function** over small dataclasses (`LineData`, `OrderData`),
   so every rule above is tested without a database.
2. `reconcile_statement()` loads lines and the relevant orders in two queries, calls
   `classify()`, then atomically replaces the statement's results and links each line to
   its order. Re-running it gives the same result (idempotent), and it refuses to run
   once results are under dispute.

**Courier balance** (`GET /api/couriers/{id}/balance/`):

```
expected_collections = Σ amount_due of DELIVERED orders
reported_collections = Σ amount_collected on reconciled lines linked to the courier's orders
fees                 = Σ fee on those lines
net_remitted         = reported_collections − fees      (what actually reached the bank)
balance_owed         = expected_collections − reported_collections
                     = expected_collections − (net_remitted + fees)
```

A positive `balance_owed` is cash the courier collected (or should have) but has not yet
accounted for: short payments plus delivered parcels missing from statements.

## Running it

### With Docker (PostgreSQL + Redis + worker)

```bash
docker compose up --build -d
docker compose exec web python manage.py seed_demo
open http://localhost:8000/api/docs/      # Swagger UI
open http://localhost:8000/admin/         # login: ops / demo-pass-123
```

Demo users (password `demo-pass-123`): `ops` (staff/superuser), `atlas` and `medina`
(merchant users).

### Tests (no infrastructure needed)

```bash
uv sync
uv run pytest --cov            # SQLite + Celery eager mode
uv run ruff check . && uv run ruff format --check .
```

Without `DATABASE_URL` the settings fall back to SQLite so tests run anywhere. CI runs the
same suite against PostgreSQL 16 (`.github/workflows/ci.yml`):

```bash
DATABASE_URL=postgres://cod:cod@localhost:5432/cod_settlement uv run pytest
```

### Local dev without Docker

```bash
cp .env.example .env
uv run python manage.py migrate
uv run python manage.py seed_demo
uv run python manage.py runserver
uv run celery -A config worker -l info   # or set CELERY_TASK_ALWAYS_EAGER=true
```

## API examples

```bash
# 1. Get a JWT
TOKEN=$(curl -s -X POST localhost:8000/api/auth/token/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"atlas","password":"demo-pass-123"}' | jq -r .access)
AUTH="Authorization: Bearer $TOKEN"

# 2. List delivered orders over 300 MAD, newest first, searching by customer
curl -s "localhost:8000/api/orders/?status=delivered&min_amount=300&ordering=-created_at&search=alaoui" -H "$AUTH"

# 3. Create an order (merchant is taken from the token, never from the body)
curl -s -X POST localhost:8000/api/orders/ -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"courier":1,"tracking_number":"SPX-900001","customer_name":"Sara Tazi","city":"Rabat","amount_due":"349.90"}'

# 4. Record a delivery attempt (moves the order to delivered)
curl -s -X POST localhost:8000/api/orders/1/attempts/ -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"outcome":"delivered"}'

# 5. Upload a courier remittance CSV -> 202 Accepted, reconciled in the background
curl -s -X POST localhost:8000/api/statements/ -H "$AUTH" \
  -F file=@fixtures/sample_remittance.csv -F courier=1 \
  -F period_start=2026-10-01 -F period_end=2026-10-31
#    uploading the identical file again -> 200 {"duplicate": true, "id": <same id>}

# 6. Reconciliation report (optionally ?outcome=amount_mismatch)
curl -s localhost:8000/api/statements/1/reconciliation/ -H "$AUTH"

# 7. What does this courier still owe me?
curl -s localhost:8000/api/couriers/1/balance/ -H "$AUTH"
# {"courier":1,"merchant":1,"currency":"MAD","delivered_orders":11,
#  "expected_collections":"6906.00","reported_collections":"6003.00","fees":"290.00",
#  "net_remitted":"5713.00","balance_owed":"903.00"}

# 8. Dispute a discrepancy (claimed_amount defaults to the full difference), then export
curl -s -X POST localhost:8000/api/disputes/ -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"result":1,"reason":"Courier short-paid 50 MAD"}'
curl -s localhost:8000/api/disputes/export/?status=open -H "$AUTH" -o disputes.csv
```

CSV format expected from couriers (header required, column order free, extra columns ignored):

```csv
tracking_number,amount_collected,fee
SPX-100003,714.00,25.00
SPX-100005,0.00,10.00
```

## Design decisions

**Money is `Decimal`, never float.** Every amount is a `DecimalField(max_digits=12,
decimal_places=2)`, parsed with `apps/common/money.py`: input with more than two decimals is
*rejected* (a courier file saying `10.005` is a data problem, not something to round
silently), totals are quantized with `ROUND_HALF_UP`, and DRF serializes money as strings
(`"349.90"`) so JavaScript clients never see a float. Tests sum thousands of amounts and
check the result to the centime.

**Idempotent uploads via SHA-256.** The upload endpoint hashes the file content; a unique
constraint on `(merchant, file_sha256)` makes re-uploads a no-op that returns the existing
statement. The constraint (not the pre-check) is the real guard, so two concurrent uploads
of the same file cannot both create statements. It is scoped per merchant so one tenant's
upload can never reveal that another tenant uploaded the same file. The Celery task itself
is also idempotent (it replaces lines/results), which makes `acks_late` redelivery safe.

**Enqueue after commit.** Requests run in a transaction (`ATOMIC_REQUESTS`), so the task is
sent with `transaction.on_commit()`. Otherwise a fast worker could look for a statement row
that is not committed yet.

**Service layer.** Business rules live in `settlement/services/`, not in views, serializers
or models. `classify()` is pure and covered by fast unit tests; views and the task only
deal with HTTP, permissions and queueing.

**Tenant isolation at the queryset level.** `MerchantScopedQuerysetMixin` filters every
queryset by the user's merchant, so a foreign ID returns **404** (it does not exist for that
user) rather than 403, which also avoids leaking which IDs exist. On writes the merchant is
taken from the authenticated user, never from the request body, and related-object fields
(e.g. disputing a reconciliation result) use tenant-filtered querysets.

**Statements are immutable.** No update/delete endpoints; corrections are a new upload. Once
a result is disputed, the statement can no longer be re-processed, so dispute evidence is
kept.

**SQLite fallback only for tests.** PostgreSQL is the target (exact `numeric` arithmetic,
row locks via `select_for_update`). SQLite keeps `uv run pytest` dependency-free; CI uses
PostgreSQL.

## License

MIT, see [LICENSE](LICENSE).
