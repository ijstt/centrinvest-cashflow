from typing import Literal
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from src.database import get_db
from src.services.recurring_service import RecurringService
from src.services.event_service import EventService, RevisionMismatchError
from src.schemas.contracts import (
    RecurringListResponse, EventListResponse, PlannedEvent, PlannedEventInput,
    EventUpdateRequest, ExternalEventsRequest, ExternalEventsResponse
)
from src.api.deps import PlatformApiException, verify_company_access
from src.models.entities import User

router = APIRouter(prefix="/api/v2/companies/{company_id}", tags=["events"])

@router.get("/recurring", response_model=RecurringListResponse)
async def list_recurring(
    company_id: str,
    user: User = Depends(verify_company_access),
    db: AsyncSession = Depends(get_db)
):
    patterns = await RecurringService.detect_recurring(db, company_id, persist=False)
    return RecurringListResponse(items=patterns)

@router.get("/events", response_model=EventListResponse)
async def list_events(
    company_id: str,
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    kind: str | None = Query(None),
    user: User = Depends(verify_company_access),
    db: AsyncSession = Depends(get_db)
):
    return await EventService.list_events(
        db=db,
        company_id=company_id,
        limit=limit,
        cursor=cursor,
        from_date=from_date,
        to_date=to_date,
        status=status_filter,
        kind=kind
    )

@router.post("/events", response_model=PlannedEvent, status_code=status.HTTP_201_CREATED)
async def create_event(
    company_id: str,
    req: PlannedEventInput,
    user: User = Depends(verify_company_access),
    db: AsyncSession = Depends(get_db)
):
    return await EventService.create_event(db, company_id, req)

@router.put("/events/{event_id}", response_model=PlannedEvent)
async def update_event(
    company_id: str,
    event_id: str,
    req: EventUpdateRequest,
    expected_data_revision: int | None = Query(None, ge=0),
    user: User = Depends(verify_company_access),
    db: AsyncSession = Depends(get_db)
):
    try:
        return await EventService.update_event(
            db=db,
            company_id=company_id,
            event_id=event_id,
            inp=req,
            expected_data_revision=expected_data_revision
        )
    except KeyError as e:
        raise PlatformApiException(
            status_code=status.HTTP_404_NOT_FOUND,
            code="not_found",
            message=str(e)
        )
    except RevisionMismatchError as e:
        raise PlatformApiException(
            status_code=status.HTTP_409_CONFLICT,
            code="conflict",
            message=str(e)
        )

@router.post("/external-events", response_model=ExternalEventsResponse)
async def import_external_events(
    company_id: str,
    req: ExternalEventsRequest,
    user: User = Depends(verify_company_access),
    db: AsyncSession = Depends(get_db)
):
    return await EventService.import_external_events(db, company_id, req)
