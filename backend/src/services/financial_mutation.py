"""Complete a financial mutation with reconciliation and forecast refresh."""
from src.services.payment_matching import reconcile_payments


async def complete_financial_change(db, company_id, changed_from_date, refresh=True):
    await db.flush()
    await reconcile_payments(db, company_id)
    await db.flush()
    if refresh:
        from src.services.reconciliation_service import refresh_latest_forecast
        await refresh_latest_forecast(db, company_id, changed_from_date)
    await db.commit()
