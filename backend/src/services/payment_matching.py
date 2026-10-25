"""Deterministic, company-scoped reconciliation; ambiguous receipts stay unmatched."""
import re
from datetime import timedelta
from decimal import Decimal
from sqlalchemy import select
from src.models.entities import PlannedEventRecord, TransactionRecord


def normalized(value):
    return re.sub(r'\W+', '', (value or '').casefold())


async def reconcile_payments(db, company_id):
    events = list((await db.execute(select(PlannedEventRecord).where(
        PlannedEventRecord.company_id == company_id,
        PlannedEventRecord.status != 'cancelled'
    ).order_by(PlannedEventRecord.due_date, PlannedEventRecord.id))).scalars())
    transactions = list((await db.execute(select(TransactionRecord).where(
        TransactionRecord.company_id == company_id,
        TransactionRecord.source_kind != 'sandbox'
    ).order_by(TransactionRecord.date, TransactionRecord.id))).scalars())
    before = [(e.id, e.status, e.paid_amount, e.matched_transaction_ids) for e in events]
    for event in events:
        event.paid_amount = Decimal('0')
        event.matched_transaction_ids = []
    for transaction in transactions:
        transaction.matched_event_id = None
        candidates = [e for e in events if e.direction == transaction.direction]
        purpose = normalized(transaction.purpose)
        referenced = [e for e in candidates if
            (e.external_id and len(normalized(e.external_id)) >= 4 and normalized(e.external_id) in purpose)
            or (len(normalized(e.title)) >= 6 and normalized(e.title) in purpose)]
        if referenced:
            candidates = referenced
        else:
            # A conflicting reference is evidence against automatic reconciliation.
            has_reference = bool(re.search(r'(?:\b\d{2,}\b|\b[a-zа-я]+[-/]\d+|№)',
                                           transaction.purpose or '', re.IGNORECASE))
            candidates = [e for e in candidates if normalized(e.counterparty_name)
                          and normalized(e.counterparty_name) == normalized(transaction.counterparty_name)
                          and abs((e.due_date - transaction.date).days) <= 30
                          and not has_reference
                          and Decimal(str(transaction.amount)) == e.amount - e.paid_amount]
        if len(candidates) != 1:
            continue
        event = candidates[0]
        event.paid_amount += Decimal(str(transaction.amount))
        event.matched_transaction_ids = [*event.matched_transaction_ids, transaction.id]
        transaction.matched_event_id = event.id
    cutoff = max((t.date for t in transactions), default=None)
    for event in events:
        if event.paid_amount >= event.amount:
            event.status = 'paid'
        elif event.paid_amount > 0:
            event.status = 'partial'
        elif cutoff and event.due_date < cutoff:
            event.status = 'overdue'
        else:
            event.status = 'planned'
    after = [(e.id, e.status, e.paid_amount, e.matched_transaction_ids) for e in events]
    return before != after
