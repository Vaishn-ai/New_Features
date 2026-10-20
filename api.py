from datetime import datetime, UTC, timedelta

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.configs.database import get_db
from src.utils.role import get_current_student
from src.modules.auth.student.models import Student
from src.modules.course.base.admin.models import Course
from src.modules.batch.base.admin.models import Batch
from src.modules.enrollments.base.student.interface import Create_Enrollment
from src.modules.enrollments.base.student.models import Enrollment, SeatHold

SESSION_EXPIRE = 10 * 60
enrollment_router = APIRouter(tags=["Enrollment"])


async def get_available_seats(batch: Batch, db: AsyncSession) -> int:
    now = datetime.now(UTC)
    result = await db.execute(
        select(func.count(SeatHold.id)).where(
            and_(
                SeatHold.batch_id == batch.id,
                SeatHold.status == "held",
                SeatHold.expires_at > now,
            )
        )
    )
    active_holds = result.scalar_one()
    return max(0, batch.seats - active_holds)


async def get_batches(current_user, db: AsyncSession):
    try:
        result = await db.execute(select(Batch))
        batches = result.scalars().all()
        batch_list = []

        for batch in batches:
            available_seats = await get_available_seats(batch, db)
            if available_seats > 0:
                batch_list.append({
                    "batch_id": batch.id,
                    "batch_name": batch.batch_name,
                    "start_date": str(batch.start_date),
                    "end_date": str(batch.end_date),
                    "starting_time": str(batch.starting_time),
                    "ending_time": str(batch.ending_time),
                    "available_seats": available_seats,
                    "course_id": batch.course_id,
                })

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "msg": "Available batches retrieved successfully",
                "batches": batch_list,
            },
        )
    except Exception as exc:
        await db.rollback()
        return JSONResponse(
            status_code=500,
            content={"success": False, "msg": "Failed to retrieve batches", "error": str(exc)},
        )


async def check_seats(batch_id: int, db: AsyncSession):
    try:
        result = await db.execute(select(Batch).where(Batch.id == batch_id))
        batch = result.scalar_one_or_none()
        if batch is None:
            return JSONResponse(status_code=404, content={"success": False, "msg": "Batch not found"})

        available_seats = await get_available_seats(batch, db)
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "msg": "Seat availability retrieved successfully",
                "data": {
                    "batch_id": batch.id,
                    "batch_name": batch.batch_name,
                    "available_seats": available_seats,
                },
            },
        )
    except Exception as exc:
        await db.rollback()
        return JSONResponse(
            status_code=500,
            content={"success": False, "msg": "Failed to check seat availability", "error": str(exc)},
        )


async def hold_seat(batch_id: int, current_user, db: AsyncSession):
    try:
        now = datetime.now(UTC)
        student_id = current_user.id

        result = await db.execute(
            select(Batch).where(Batch.id == batch_id).with_for_update()
        )
        batch = result.scalar_one_or_none()
        if batch is None:
            await db.rollback()
            return JSONResponse(status_code=404, content={"success": False, "msg": "Batch not found"})

        existing_result = await db.execute(
            select(SeatHold).where(
                SeatHold.student_id == student_id,
                SeatHold.batch_id == batch_id,
                SeatHold.status == "held",
                SeatHold.expires_at > now,
            )
        )
        existing_hold = existing_result.scalar_one_or_none()
        if existing_hold is not None:
            response = {
                "success": True,
                "msg": "You already hold a seat in this batch",
                "hold_id": existing_hold.id,
                "expires_at": existing_hold.expires_at.isoformat(),
            }
            await db.rollback()
            return JSONResponse(status_code=200, content=response)

        count_result = await db.execute(
            select(func.count(SeatHold.id)).where(
                and_(
                    SeatHold.batch_id == batch_id,
                    SeatHold.status == "held",
                    SeatHold.expires_at > now,
                )
            )
        )
        active_holds = count_result.scalar_one()
        available_seats = max(0, batch.seats - active_holds)
        if available_seats <= 0:
            await db.rollback()
            return JSONResponse(status_code=409, content={"success": False, "msg": "No seats available in this batch"})

        hold = SeatHold(
            student_id=student_id,
            batch_id=batch_id,
            status="held",
            expires_at=now + timedelta(seconds=SESSION_EXPIRE),
        )
        db.add(hold)
        await db.flush()
        response = {
            "success": True,
            "msg": "Seat held successfully for 10 minutes",
            "hold_id": hold.id,
            "batch_id": batch_id,
            "expires_at": hold.expires_at.isoformat(),
            "available_seats": available_seats - 1,
        }
        await db.commit()
        return JSONResponse(status_code=201, content=response)
    except Exception as exc:
        await db.rollback()
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"success": False, "msg": "Failed to hold seat", "error": str(exc)})


async def enroll_for_course(data: Create_Enrollment, current_user, db: AsyncSession):
    try:
        now = datetime.now(UTC)
        student_id = current_user.id

        batch_result = await db.execute(
            select(Batch).where(Batch.id == data.batch_id).with_for_update()
        )
        batch = batch_result.scalar_one_or_none()
        if batch is None:
            await db.rollback()
            return JSONResponse(status_code=404, content={"success": False, "msg": "Batch not found"})

        if data.student_id != student_id:
            await db.rollback()
            return JSONResponse(status_code=403, content={"success": False, "msg": "You can enroll only yourself"})

        if batch.course_id != data.course_id:
            await db.rollback()
            return JSONResponse(status_code=400, content={"success": False, "msg": "Course does not belong to this batch"})

        if batch.seats <= 0:
            await db.rollback()
            return JSONResponse(status_code=409, content={"success": False, "msg": "No seats available"})

        student_result = await db.execute(select(Student).where(Student.id == student_id))
        if student_result.scalar_one_or_none() is None:
            await db.rollback()
            return JSONResponse(status_code=404, content={"success": False, "msg": "Student not found"})

        course_result = await db.execute(select(Course).where(Course.id == data.course_id))
        if course_result.scalar_one_or_none() is None:
            await db.rollback()
            return JSONResponse(status_code=404, content={"success": False, "msg": "Course not found"})

        hold_result = await db.execute(
            select(SeatHold).where(
                SeatHold.student_id == student_id,
                SeatHold.batch_id == data.batch_id,
                SeatHold.status == "held",
                SeatHold.expires_at > now,
            ).with_for_update()
        )
        hold = hold_result.scalar_one_or_none()
        if hold is None:
            await db.rollback()
            return JSONResponse(
                status_code=409,
                content={"success": False, "msg": "No active seat hold found. Please hold a seat again."},
            )

        existing_result = await db.execute(
            select(Enrollment).where(
                Enrollment.student_id == student_id,
                Enrollment.batch_id == data.batch_id,
                Enrollment.active_at.is_(True),
            )
        )
        if existing_result.scalar_one_or_none() is not None:
            await db.rollback()
            return JSONResponse(status_code=409, content={"success": False, "msg": "Already enrolled in this batch"})

        db.add(Enrollment(student_id=student_id, course_id=data.course_id, batch_id=data.batch_id))
        batch.seats -= 1
        hold.status = "confirmed"
        await db.commit()

        return JSONResponse(
            status_code=201,
            content={
                "success": True,
                "msg": "Enrollment confirmed successfully",
                "batch_id": batch.id,
                "remaining_seats": batch.seats,
            },
        )
    except Exception as exc:
        await db.rollback()
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"success": False, "msg": "Enrollment confirmation failed", "error": str(exc)})


@enrollment_router.get("/enrollments/seats")
async def seats(batch_id: int, db: AsyncSession = Depends(get_db)):
    return await check_seats(batch_id=batch_id, db=db)


@enrollment_router.get("/view-Batch")
async def view_batches(current_user=Depends(get_current_student), db: AsyncSession = Depends(get_db)):
    return await get_batches(current_user=current_user, db=db)


@enrollment_router.post("/enrollments/hold")
async def hold_batch_seat(batch_id: int, current_user=Depends(get_current_student), db: AsyncSession = Depends(get_db)):
    return await hold_seat(batch_id=batch_id, current_user=current_user, db=db)


@enrollment_router.post("/enrollment")
async def enroll(data: Create_Enrollment, current_user=Depends(get_current_student), db: AsyncSession = Depends(get_db)):
    return await enroll_for_course(data=data, current_user=current_user, db=db)
