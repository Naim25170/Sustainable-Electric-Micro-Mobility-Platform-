from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status

from app.deps import get_current_user_id
from app.db import get_pool
from app.schemas import PurchasePackageRequest
from app.util_json import record_to_dict

router = APIRouter(prefix="/wallet", tags=["wallet"])
PRICE_PER_MINUTE = {
    "ebike": 0.10,
    "scooter": 0.20,
    "car": 0.50,
}

@router.get("/packages")
async def list_packages(_user_id: UUID = Depends(get_current_user_id)):
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """
                SELECT * FROM ride_packages
                WHERE coalesce(is_active, true) = true
                ORDER BY price ASC NULLS LAST
                """
            )
        except asyncpg.UndefinedTableError:
            return []
    return [record_to_dict(r) for r in rows]


@router.get("/overview")
async def wallet_overview(user_id: UUID = Depends(get_current_user_id)):
    pool = get_pool()
    async with pool.acquire() as conn:
        pkg_rows = await conn.fetch(
            """
            SELECT up.*, p.title, p.ride_credits, p.price, p.currency
            FROM user_package_purchases up
            LEFT JOIN ride_packages p ON p.package_id = up.package_id
            WHERE up.user_id = $1
            ORDER BY up.created_at DESC NULLS LAST
            """,
            user_id,
        )
        try:
            methods = await conn.fetch(
                """
                SELECT * FROM payment_methods
                WHERE user_id = $1
                ORDER BY coalesce(is_default, false) DESC
                """,
                user_id,
            )
        except asyncpg.UndefinedTableError:
            methods = []
        payments = await conn.fetch(
            """
            SELECT * FROM payments
            WHERE user_id = $1
            ORDER BY created_at DESC NULLS LAST
            LIMIT 20
            """,
            user_id,
        )

    purchases = []
    for r in pkg_rows:
        d = record_to_dict(r)
        purchases.append(
            {
                **d,
                "ride_packages": {
                    "title": d.get("title"),
                    "ride_credits": d.get("ride_credits"),
                    "price": d.get("price"),
                    "currency": d.get("currency"),
                },
            }
        )
    return {
        "purchases": purchases,
        "payment_methods": [record_to_dict(r) for r in methods],
        "recent_payments": [record_to_dict(r) for r in payments],
    }


@router.post("/purchase")
async def purchase(body: PurchasePackageRequest, user_id: UUID = Depends(get_current_user_id)):
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            pkg = await conn.fetchrow(
                "SELECT package_id, price, currency, ride_credits FROM ride_packages WHERE package_id = $1",
                body.package_id,
            )
            if not pkg:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")
            pay = await conn.fetchrow(
                """
                INSERT INTO payments (payment_id, user_id, amount, currency, status, method, created_at)
                VALUES (gen_random_uuid(), $1, $2, $3, 'completed', 'in_app', now())
                RETURNING payment_id
                """,
                user_id,
                float(pkg["price"] or 0),
                pkg["currency"] or "USD",
            )
            pay_id = pay["payment_id"]
            try:
                await conn.execute(
                    """
                    INSERT INTO user_package_purchases (purchase_id, user_id, package_id, rides_remaining, payment_id, created_at)
                    VALUES (gen_random_uuid(), $1, $2, $3, $4, now())
                    """,
                    user_id,
                    body.package_id,
                    int(pkg["ride_credits"] or 0),
                    pay_id,
                )
            except asyncpg.UndefinedTableError:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Missing ride_packages or user_package_purchases tables",
                )
    return {"ok": True, "payment_id": str(pay_id)}
async def charge_user_for_ride(conn, user_id, vehicle_type, duration_minutes):
    price_per_min = PRICE_PER_MINUTE.get(vehicle_type, 0.2)
    total_cost = round(duration_minutes * price_per_min, 2)

    await conn.execute(
        """
        INSERT INTO payments (payment_id, user_id, amount, currency, status, method, created_at)
        VALUES (gen_random_uuid(), $1, $2, 'USD', 'completed', 'ride_charge', now())
        """,
        user_id,
        total_cost,
    )

    return total_cost