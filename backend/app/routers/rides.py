import logging
import math
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.db import get_pool
from app.deps import get_current_user_id
from app.schemas import EndRideRequest, StartRideRequest
from app.services.reservations import (
    convert_active_reservation_to_ride,
    expire_due_reservations,
    find_active_reservation_for_vehicle,
)
from app.util_json import record_to_dict
from app.routers.wallet import charge_user_for_ride

def calculate_distance_m(lat1, lng1, lat2, lng2):
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rides", tags=["rides"])


def _ride_row_dict(record) -> dict:
    """Map DB column names to the API shape the mobile client expects."""
    d = record_to_dict(record)
    if d.get("start_time") is None and d.get("started_at") is not None:
        d["start_time"] = d["started_at"]
    if d.get("end_time") is None and d.get("ended_at") is not None:
        d["end_time"] = d["ended_at"]
    if d.get("cost") is None and d.get("total_cost") is not None:
        d["cost"] = d["total_cost"]
    return d


@router.get("/me/active")
async def active_ride(user_id: UUID = Depends(get_current_user_id)):
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                SELECT
                  r.*,
                  NULL::text AS model,
                  v.type::text AS type,
                  NULL::text AS qr_code
                FROM rides r
                LEFT JOIN vehicles v ON v.vehicle_id = r.vehicle_id
                WHERE r.user_id = $1
                  AND r.status = 'started'
                ORDER BY r.started_at DESC NULLS LAST
                LIMIT 1
                """,
                user_id,
            )
        except Exception as exc:
            logger.exception("Failed to query active ride for user %s", user_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Database error checking active ride: {exc}",
            ) from exc

    if not row:
        return None

    d = _ride_row_dict(row)
    return {
        "ride_id": d["ride_id"],
        "user_id": d["user_id"],
        "vehicle_id": d["vehicle_id"],
        "start_time": d.get("start_time"),
        "end_time": d.get("end_time"),
        "start_lat": d.get("start_lat"),
        "start_lng": d.get("start_lng"),
        "end_lat": d.get("end_lat"),
        "end_lng": d.get("end_lng"),
        "distance_meters": d.get("distance_meters"),
        "status": d.get("status"),
        "cost": d.get("cost"),
        "vehicles": {
            "model": d.get("model"),
            "type": d.get("type"),
            "qr_code": d.get("qr_code"),
        },
    }


@router.get("/me")
async def list_my_rides(user_id: UUID = Depends(get_current_user_id)):
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """
                SELECT *
                FROM rides
                WHERE user_id = $1
                ORDER BY started_at DESC NULLS LAST
                """,
                user_id,
            )
        except Exception as exc:
            logger.exception("Failed to list rides for user %s", user_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Database error listing rides: {exc}",
            ) from exc

    return [_ride_row_dict(r) for r in rows]


@router.post("/start")
async def start_ride(body: StartRideRequest, user_id: UUID = Depends(get_current_user_id)):
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await expire_due_reservations(conn)

            active = await conn.fetchrow(
                """
                SELECT ride_id FROM rides
                WHERE user_id = $1 AND status = 'started'
                LIMIT 1
                """,
                user_id,
            )
            if active:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="You already have an active ride. End it before starting another.",
                )

            vrow = await conn.fetchrow(
                """
                SELECT availability_status
                FROM vehicles
                WHERE vehicle_id = $1
                FOR UPDATE
                """,
             body.vehicle_id,
            )
            if not vrow:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Vehicle not found",
                )

            vehicle_availability = (str(vrow["availability_status"]) or "").lower()
            state_row = await conn.fetchrow(
                """
                SELECT current_lat, current_lng
                FROM vehicle_current_state
                WHERE vehicle_id = $1
                """,
                body.vehicle_id,
            )

            if not state_row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Vehicle location not found",
                )

            vehicle_lat = state_row["current_lat"]
            vehicle_lng = state_row["current_lng"]

            distance = calculate_distance_m(
                body.start_lat,
                body.start_lng,
                vehicle_lat,
                vehicle_lng,
            )

            if distance > 5:
                raise HTTPException(
                    status_code=400,
                    detail="You must be within 5 meters of the vehicle",
                )

            reservation = await find_active_reservation_for_vehicle(
                conn, vehicle_id=body.vehicle_id
            )

            if vehicle_availability == "reserved":
                if not reservation:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Vehicle is not available",
                    )
                if reservation["user_id"] != user_id:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Vehicle is currently reserved by another user",
                    )

            elif vehicle_availability == "available":
                if reservation and reservation["user_id"] != user_id:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Vehicle is currently reserved by another user",
                    )

            else:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Vehicle is not available",
                )

            await conn.execute(
                """
                INSERT INTO rides (ride_id, user_id, vehicle_id, started_at, status, start_lat, start_lng)
                VALUES (gen_random_uuid(), $1, $2, now(), 'started', $3, $4)
                """,
                user_id,
                body.vehicle_id,
                body.start_lat,
                body.start_lng,
            )

            updated_vehicle = await conn.execute(
                """
                UPDATE vehicles
                SET availability_status = 'in_use'
                WHERE vehicle_id = $1
                  AND availability_status IN ('available', 'reserved')
                """,
                body.vehicle_id,
            )
            if updated_vehicle != "UPDATE 1":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Vehicle is not available",
                )

            await convert_active_reservation_to_ride(
                conn,
                user_id=user_id,
                vehicle_id=body.vehicle_id,
            )

            await conn.execute(
                """
                UPDATE vehicle_current_state
                SET updated_at = now()
                WHERE vehicle_id = $1
                """,
                body.vehicle_id,
            )

    return {"ok": True}


@router.post("/end")
async def end_ride(body: EndRideRequest, user_id: UUID = Depends(get_current_user_id)):
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT ride_id, vehicle_id, user_id, status
                FROM rides
                WHERE ride_id = $1
                FOR UPDATE
                """,
                body.ride_id,
            )
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Ride not found",
                )
            if row["user_id"] != user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not your ride",
                )
            if str(row["status"] or "").lower() not in ("started", "paused"):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Ride is not active",
                )

            vehicle_id = row["vehicle_id"]
            vehicle_row = await conn.fetchrow(
                 """
                SELECT type
                FROM vehicles
                WHERE vehicle_id = $1
                """,
                vehicle_id,
            )

            ride_row = await conn.fetchrow(
                """
                SELECT started_at
                FROM rides
                WHERE ride_id = $1
                """,
                body.ride_id,
            )

            if not vehicle_row or not ride_row or not ride_row["started_at"]:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Missing ride or vehicle information for pricing",
                )

            duration_row = await conn.fetchrow(
                """
                SELECT EXTRACT(EPOCH FROM (now() - started_at)) / 60 AS minutes
                FROM rides
                WHERE ride_id = $1
                """,
                body.ride_id,
            )

            duration_minutes = float(duration_row["minutes"] or 0)

            total_cost = await charge_user_for_ride(
                conn,
                user_id,
                str(vehicle_row["type"]),
                duration_minutes,
            )

            await conn.execute(
                """
                UPDATE rides
                SET ended_at = now(),
                status = 'completed',
                end_lat = $1,
                end_lng = $2,
                total_cost = $3
                WHERE ride_id = $4
                """,
                body.end_lat,
                body.end_lng,
                total_cost,
                body.ride_id,
            )
           

            await conn.execute(
                "UPDATE vehicles SET availability_status = 'available' WHERE vehicle_id = $1",
                vehicle_id,
            )

            await conn.execute(
                """
                UPDATE vehicle_current_state
                SET updated_at = now()
                WHERE vehicle_id = $1
                """,
                vehicle_id,
            )

    return {"ok": True}