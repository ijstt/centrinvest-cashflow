"""Update future commitments using a validated cached statistical baseline."""
from datetime import date
from sqlalchemy import select
from src.models.entities import Company, ForecastSnapshotRecord
from src.schemas.contracts import ForecastUpdateRequest, ForecastUpdateResponse, CreateForecastRequest
from src.services.forecaster_service import ForecasterService


class IncrementalService:
    @staticmethod
    async def update_forecast(db, company_id, forecast_id, req: ForecastUpdateRequest):
        company = (await db.execute(select(Company).where(
            Company.id == company_id).with_for_update())).scalar_one_or_none()
        if not company:
            raise KeyError(f'Company {company_id} not found')
        if company.data_revision != req.expected_data_revision:
            raise ValueError('conflict: Revision mismatch')
        previous = (await db.execute(select(ForecastSnapshotRecord).where(
            ForecastSnapshotRecord.company_id == company_id,
            ForecastSnapshotRecord.id == forecast_id))).scalar_one_or_none()
        if not previous:
            raise KeyError(f'Forecast {forecast_id} not found')
        state = previous.calculation_state or {}
        original = state.get('request', {})
        cutoff = previous.as_of_date
        known_balance_date = date.fromisoformat(state.get('balance_date') or cutoff.isoformat())
        if company.balance_date and company.balance_date > known_balance_date:
            cutoff = max(cutoff, company.balance_date)
        request = CreateForecastRequest(
            as_of_date=cutoff.isoformat(), horizon_days=previous.horizon_days,
            initial_balance=original.get('initial_balance'),
            include_planned_events=original.get('include_planned_events', True),
            model=original.get('model', previous.model),
            expected_data_revision=company.data_revision)
        reuse = state if (cutoff == previous.as_of_date
            and date.fromisoformat(req.changed_from_date) > cutoff) else None
        forecast = await ForecasterService.create_forecast(db, company_id, request, reusable_state=reuse)
        saved = await db.get(ForecastSnapshotRecord, forecast.id)
        reused = bool(reuse and saved.calculation_state['history_key'] == state.get('history_key'))
        return ForecastUpdateResponse(forecast=forecast,
            mode='incremental' if reused else 'full_retrain',
            recomputed_from_date=req.changed_from_date,
            reason=('Использована сохраненная статистическая модель; обновлены события и риск.' if reused
                    else 'Изменились исторические данные или отсутствует сохраненная модель; выполнен полный расчет.'))
