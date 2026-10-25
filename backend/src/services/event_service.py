from datetime import datetime, date, timezone, timedelta
from sqlalchemy import select, and_, desc, func
from sqlalchemy.ext.asyncio import AsyncSession
from src.models.entities import Company, PlannedEventRecord
from src.schemas.contracts import (
    PlannedEventInput, PlannedEvent, EventUpdateRequest, EventListResponse,
    ExternalEventsRequest, ExternalEventsResponse, SourceReference
)
from src.services.statement_service import sanitize_html_tags
from src.services.financial_mutation import complete_financial_change

# Official Russian Government weekend transfers (Постановления Правительства РФ 2024-2026)
GOVERNMENT_TRANSFERS_HOLIDAYS = {
    # 2024 (Постановление Правительства РФ № 1314)
    date(2024, 4, 29), date(2024, 4, 30), date(2024, 5, 10), date(2024, 12, 30), date(2024, 12, 31),
    # 2025 (Постановление Правительства РФ № 1335)
    date(2025, 5, 2), date(2025, 5, 8), date(2025, 6, 13), date(2025, 11, 3), date(2025, 12, 31),
    # 2026
    date(2026, 1, 9), date(2026, 3, 9), date(2026, 5, 8), date(2026, 5, 11), date(2026, 12, 31),
}
GOVERNMENT_TRANSFERS_WORKING_WEEKENDS = {
    # 2024
    date(2024, 4, 27), date(2024, 11, 2), date(2024, 12, 28),
    # 2025
    date(2025, 11, 1),
}

def is_russian_holiday(d: date) -> bool:
    if d in GOVERNMENT_TRANSFERS_HOLIDAYS:
        return True
    if d.month == 1 and 1 <= d.day <= 8:
        return True
    if d.month == 2 and d.day == 23:
        return True
    if d.month == 3 and d.day == 8:
        return True
    if d.month == 5 and (d.day == 1 or d.day == 9):
        return True
    if d.month == 6 and d.day == 12:
        return True
    if d.month == 11 and d.day == 4:
        return True
    return False

def is_russian_working_day(d: date) -> bool:
    if d in GOVERNMENT_TRANSFERS_WORKING_WEEKENDS:
        return True
    if is_russian_holiday(d):
        return False
    return d.weekday() < 5

def adjust_tax_due_date_art_6_1(d: date) -> date:
    curr = d
    while not is_russian_working_day(curr):
        curr += timedelta(days=1)
    return curr

def parse_date(val: str | date | datetime) -> date:
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    s = str(val).strip()
    if len(s) >= 10:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            pass
    return datetime.fromisoformat(s.replace("Z", "+00:00")).date()


class RevisionMismatchError(ValueError):
    def __init__(self, message: str = "Revision mismatch", expected: int | None = None, actual: int | None = None):
        super().__init__(message)
        self.message = message
        self.expected = expected
        self.actual = actual
        self.status_code = 409


def event_response(record):
    return PlannedEvent(
        id=record.id, company_id=record.company_id, title=record.title, kind=record.kind,
        direction=record.direction, amount=float(record.amount), due_date=record.due_date.isoformat(),
        category_id=record.category_id, counterparty_name=record.counterparty_name,
        on_time_probability=record.on_time_probability,
        source=SourceReference(kind=record.source_kind,source_id=record.source_id,
            external_id=record.external_id,label=record.source_label or ''),
        status=record.status,paid_amount=float(record.paid_amount),
        matched_transaction_ids=record.matched_transaction_ids or [],
        recurrence_id=record.recurrence_id,updated_at=record.updated_at.isoformat())


class EventService:
    @staticmethod
    async def list_events(
        db: AsyncSession,
        company_id: str,
        limit: int = 50,
        cursor: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        status: str | None = None,
        kind: str | None = None
    ) -> EventListResponse:
        conditions = [PlannedEventRecord.company_id == company_id]
        if from_date:
            conditions.append(PlannedEventRecord.due_date >= parse_date(from_date))
        if to_date:
            conditions.append(PlannedEventRecord.due_date <= parse_date(to_date))
        if status:
            conditions.append(PlannedEventRecord.status == status)
        if kind:
            conditions.append(PlannedEventRecord.kind == kind)

        q = (
            select(PlannedEventRecord)
            .where(and_(*conditions))
            .order_by(PlannedEventRecord.due_date, PlannedEventRecord.id)
            .limit(limit)
        )
        if cursor and cursor.isdigit():
            q = q.offset(int(cursor))

        res = await db.execute(q)
        records = res.scalars().all()

        items = []
        for r in records:
            items.append(PlannedEvent(
                id=r.id,
                company_id=r.company_id,
                title=r.title,
                kind=r.kind,
                direction=r.direction,
                amount=float(r.amount),
                due_date=r.due_date.strftime("%Y-%m-%d"),
                category_id=r.category_id,
                counterparty_name=r.counterparty_name,
                on_time_probability=r.on_time_probability,
                source=SourceReference(
                    kind=r.source_kind,
                    source_id=r.source_id,
                    external_id=r.external_id,
                    label=r.source_label or r.title
                ),
                status=r.status,
                paid_amount=float(r.paid_amount),
                matched_transaction_ids=r.matched_transaction_ids or [],
                recurrence_id=r.recurrence_id,
                updated_at=r.updated_at.isoformat() if r.updated_at else datetime.now(timezone.utc).isoformat()
            ))

        next_cursor = None
        cur_offset = int(cursor) if cursor and cursor.isdigit() else 0
        if len(items) == limit:
            next_cursor = str(cur_offset + limit)

        return EventListResponse(items=items, next_cursor=next_cursor)

    @staticmethod
    async def create_event(
        db: AsyncSession,
        company_id: str,
        inp: PlannedEventInput
    ) -> PlannedEvent:
        comp_res = await db.execute(select(Company).where(Company.id == company_id).with_for_update())
        comp = comp_res.scalar_one_or_none()
        if not comp:
            raise KeyError(f"Company {company_id} not found")

        rec = PlannedEventRecord(
            company_id=company_id,
            title=sanitize_html_tags(inp.title) or "",
            kind=inp.kind,
            direction=inp.direction,
            amount=inp.amount,
            due_date=parse_date(inp.due_date),
            category_id=inp.category_id,
            counterparty_name=sanitize_html_tags(inp.counterparty_name),
            on_time_probability=inp.on_time_probability,
            source_kind=inp.source.kind,
            source_id=inp.source.source_id,
            source_label=sanitize_html_tags(inp.source.label) or "",
            external_id=inp.source.external_id,
            status="planned",
            paid_amount=0.0
        )
        db.add(rec)
        comp.data_revision += 1
        await complete_financial_change(db, company_id, rec.due_date.isoformat())
        await db.refresh(rec)

        return event_response(rec)

    @staticmethod
    async def update_event(
        db: AsyncSession,
        company_id: str,
        event_id: str,
        inp: EventUpdateRequest,
        expected_data_revision: int | None = None
    ) -> PlannedEvent:
        comp_res = await db.execute(select(Company).where(Company.id == company_id).with_for_update())
        comp = comp_res.scalar_one_or_none()
        if not comp:
            raise KeyError(f"Company {company_id} not found")

        res = await db.execute(
            select(PlannedEventRecord).where(
                PlannedEventRecord.company_id == company_id,
                PlannedEventRecord.id == event_id
            )
        )
        rec = res.scalar_one_or_none()
        if not rec:
            raise KeyError(f"Event {event_id} not found")

        # Optimistic locking check
        exp_rev = expected_data_revision if expected_data_revision is not None else getattr(inp, "expected_data_revision", None)
        if exp_rev is not None and exp_rev != comp.data_revision:
            raise RevisionMismatchError(
                f"conflict: Revision mismatch. Expected {exp_rev}, found {comp.data_revision}",
                expected=exp_rev,
                actual=comp.data_revision
            )

        rec.title = sanitize_html_tags(inp.title) or ""
        rec.kind = inp.kind
        rec.direction = inp.direction
        rec.amount = inp.amount
        rec.due_date = parse_date(inp.due_date)
        rec.category_id = inp.category_id
        rec.counterparty_name = sanitize_html_tags(inp.counterparty_name)
        rec.on_time_probability = inp.on_time_probability
        rec.source_kind = inp.source.kind
        rec.source_id = inp.source.source_id
        rec.source_label = sanitize_html_tags(inp.source.label) or ""
        rec.external_id = inp.source.external_id
        if inp.cancelled:
            rec.status = "cancelled"

        comp.data_revision += 1

        await complete_financial_change(db, company_id, rec.due_date.isoformat())
        await db.refresh(rec)

        return event_response(rec)

    @staticmethod
    async def import_external_events(
        db: AsyncSession,
        company_id: str,
        req: ExternalEventsRequest
    ) -> ExternalEventsResponse:
        comp_res = await db.execute(select(Company).where(Company.id == company_id).with_for_update())
        comp = comp_res.scalar_one_or_none()
        if not comp:
            raise KeyError(f"Company {company_id} not found")

        created_events: list[PlannedEvent] = []
        updated_count = 0
        duplicate_count = 0

        # Existing events map by (source_kind, external_id)
        existing_res = await db.execute(
            select(PlannedEventRecord).where(
                PlannedEventRecord.company_id == company_id,
                PlannedEventRecord.external_id.isnot(None)
            )
        )
        existing_map = {(r.source_kind, r.external_id): r for r in existing_res.scalars().all()}

        # 1. Invoices
        for inv in req.invoices:
            key = ("invoice", inv.external_id)
            if key in existing_map:
                existing = existing_map[key]
                if existing.amount == inv.amount and existing.due_date.strftime("%Y-%m-%d") == inv.due_date:
                    duplicate_count += 1
                else:
                    existing.amount = inv.amount
                    existing.due_date = parse_date(inv.due_date)
                    existing.title = sanitize_html_tags(f"Счет № {inv.number}") or ""
                    updated_count += 1
            else:
                rec = PlannedEventRecord(
                    company_id=company_id,
                    title=sanitize_html_tags(f"Счет № {inv.number}") or "",
                    kind="invoice",
                    direction=inv.direction,
                    amount=inv.amount,
                    due_date=parse_date(inv.due_date),
                    category_id=inv.category_id,
                    counterparty_name=sanitize_html_tags(inv.counterparty_name),
                    on_time_probability=inv.on_time_probability,
                    source_kind="invoice",
                    source_id=req.source.source_id,
                    source_label=sanitize_html_tags(req.source.label or f"Счет {inv.number}") or "",
                    external_id=inv.external_id,
                    status="planned"
                )
                db.add(rec)
                await db.flush()
                existing_map[key] = rec
                created_events.append(PlannedEvent(
                    id=rec.id,
                    company_id=company_id,
                    title=rec.title,
                    kind="invoice",
                    direction=rec.direction,
                    amount=float(rec.amount),
                    due_date=rec.due_date.strftime("%Y-%m-%d"),
                    category_id=rec.category_id,
                    counterparty_name=rec.counterparty_name,
                    on_time_probability=rec.on_time_probability,
                    source=SourceReference(kind="invoice", source_id=req.source.source_id, external_id=inv.external_id, label=rec.source_label),
                    status=rec.status,
                    paid_amount=0.0,
                    matched_transaction_ids=[],
                    recurrence_id=None,
                    updated_at=datetime.now(timezone.utc).isoformat()
                ))

        # 2. Contracts
        for ctr in req.contracts:
            for idx, item in enumerate(ctr.schedule, start=1):
                sub_ext_id = f"{ctr.external_id}_stage_{idx}"
                key = ("contract", sub_ext_id)
                if key in existing_map:
                    duplicate_count += 1
                else:
                    rec = PlannedEventRecord(
                        company_id=company_id,
                        title=sanitize_html_tags(f"{ctr.title} (этап {idx})") or "",
                        kind="contract",
                        direction=ctr.direction,
                        amount=item.amount,
                        due_date=parse_date(item.due_date),
                        category_id=ctr.category_id,
                        counterparty_name=sanitize_html_tags(ctr.counterparty_name),
                        on_time_probability=item.on_time_probability,
                        source_kind="contract",
                        source_id=req.source.source_id,
                        source_label=sanitize_html_tags(f"{ctr.title} / {ctr.payment_terms}") or "",
                        external_id=sub_ext_id,
                        status="planned"
                    )
                    db.add(rec)
                    await db.flush()
                    existing_map[key] = rec
                    created_events.append(PlannedEvent(
                        id=rec.id,
                        company_id=company_id,
                        title=rec.title,
                        kind="contract",
                        direction=rec.direction,
                        amount=float(rec.amount),
                        due_date=rec.due_date.strftime("%Y-%m-%d"),
                        category_id=rec.category_id,
                        counterparty_name=rec.counterparty_name,
                        on_time_probability=rec.on_time_probability,
                        source=SourceReference(kind="contract", source_id=req.source.source_id, external_id=sub_ext_id, label=rec.source_label),
                        status=rec.status,
                        paid_amount=0.0,
                        matched_transaction_ids=[],
                        recurrence_id=None,
                        updated_at=datetime.now(timezone.utc).isoformat()
                    ))

        # 3. Taxes
        for tax in req.taxes:
            if not tax.confirmed_by_user:
                continue
            tax_amt = float(tax.amount)
            if tax_amt <= 0:
                raise ValueError("validation_error: tax amount must be strictly positive")

            raw_due = parse_date(tax.due_date)
            adjusted_due = adjust_tax_due_date_art_6_1(raw_due)

            key = ("tax", tax.external_id)
            if key in existing_map:
                duplicate_count += 1
            else:
                rec = PlannedEventRecord(
                    company_id=company_id,
                    title=sanitize_html_tags(f"{tax.title} ({tax.period})") or "",
                    kind="tax",
                    direction="expense",
                    amount=tax_amt,
                    due_date=adjusted_due,
                    category_id="taxes",
                    counterparty_name="ФНС России (ЕНП)",
                    on_time_probability=1.0,
                    source_kind="tax",
                    source_id=req.source.source_id,
                    source_label=f"Налоговый календарь ({tax.period})",
                    external_id=tax.external_id,
                    status="planned"
                )
                db.add(rec)
                await db.flush()
                existing_map[key] = rec
                created_events.append(PlannedEvent(
                    id=rec.id,
                    company_id=company_id,
                    title=rec.title,
                    kind="tax",
                    direction=rec.direction,
                    amount=float(rec.amount),
                    due_date=rec.due_date.strftime("%Y-%m-%d"),
                    category_id="taxes",
                    counterparty_name="ФНС России (ЕНП)",
                    on_time_probability=1.0,
                    source=SourceReference(kind="tax", source_id=req.source.source_id, external_id=tax.external_id, label=rec.source_label),
                    status=rec.status,
                    paid_amount=0.0,
                    matched_transaction_ids=[],
                    recurrence_id=None,
                    updated_at=datetime.now(timezone.utc).isoformat()
                ))

        changed = bool(created_events or updated_count)
        if changed:
            comp.data_revision += 1
        dates = [event.due_date for event in created_events]
        await complete_financial_change(db, company_id, min(dates) if dates else
            (comp.balance_date or date.today()).isoformat(), refresh=changed)
        if created_events:
            records = (await db.execute(select(PlannedEventRecord).where(
                PlannedEventRecord.company_id == company_id,
                PlannedEventRecord.id.in_([event.id for event in created_events])))).scalars().all()
            created_events = [event_response(record) for record in records]

        return ExternalEventsResponse(
            created_events=created_events,
            updated_count=updated_count,
            duplicate_count=duplicate_count,
            data_revision=comp.data_revision
        )


def calculate_usn_advance(
    income: float,
    expense: float = 0.0,
    rate_type: str = "income",
    rate: float | None = None
) -> float:
    """
    Automated USN tax advance calculation for Russian tax regime.
    - rate_type='income': default rate 6% on cumulative revenue
    - rate_type='income_minus_expense': default rate 15% on max(0, income - expense)
    """
    if rate is None:
        effective_rate = 0.06 if rate_type == "income" else 0.15
    else:
        effective_rate = rate if rate <= 1.0 else (rate / 100.0)

    if rate_type == "income":
        tax_base = max(0.0, float(income))
    else:
        tax_base = max(0.0, float(income) - float(expense))

    return round(tax_base * effective_rate, 2)


def calculate_quarterly_vat(
    taxable_sales: float,
    deductible_purchases: float = 0.0,
    vat_rate: float = 0.20
) -> float:
    """
    Automated VAT calculation for quarterly Russian VAT declaration.
    Default VAT rate 20%.
    """
    effective_rate = vat_rate if vat_rate <= 1.0 else (vat_rate / 100.0)
    net_taxable = max(0.0, float(taxable_sales) - float(deductible_purchases))
    return round(net_taxable * effective_rate, 2)


def calculate_enp_tax(tax_components: list[float] | dict[str, float]) -> float:
    """
    Automated ENP (Unified Tax Payment) aggregation.
    Sums upcoming tax obligations into a single consolidated ENP obligation.
    """
    if isinstance(tax_components, dict):
        amounts = [float(v) for v in tax_components.values() if v is not None]
    else:
        amounts = [float(v) for v in tax_components if v is not None]
    return round(max(0.0, sum(amounts)), 2)

