"""Release acceptance checks for financial invariants, using an isolated in-memory ledger."""
from datetime import date, timedelta
from types import SimpleNamespace
import numpy as np
import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
from src.database import Base
from src.models.entities import Company, DailyActualRecord
from src.schemas.contracts import CreateForecastRequest, PlannedEventInput, EventUpdateRequest, ForecastUpdateRequest
from src.schemas.contracts import ColumnMapping
from src.services.forecaster_service import ForecasterService
from src.services.event_service import EventService
from src.services.scenario_service import ScenarioService
from src.services.incremental_service import IncrementalService
from src.services.statement_service import StatementService
from src.ml.forecaster import FinancialForecaster


@pytest_asyncio.fixture
async def ledger():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:', poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(Company(id='audit', name='Audit', business_type='Services', data_revision=1,
                       closing_balance=100, balance_date=date(2026, 10, 1)))
        for offset in range(42):
            db.add(DailyActualRecord(company_id='audit', date=date(2026,10,1)-timedelta(days=offset),
                                    income=100, expense=100, closing_balance=100,
                                    transaction_count=2, observation='observed'))
        await db.commit()
        yield db
    await engine.dispose()


def event(**changes):
    values = dict(title='New obligation', kind='tax', direction='expense', amount=40,
                  due_date='2026-10-02', category_id='taxes', on_time_probability=0.8,
                  source=dict(kind='manual', source_id=None, external_id=None, label='Audit input'))
    return PlannedEventInput(**(values | changes))


def forecast_request(revision, **changes):
    values=dict(as_of_date='2026-10-01', horizon_days=30, initial_balance=None,
                include_planned_events=True, model='weekday_mean', expected_data_revision=revision)
    return CreateForecastRequest(**(values | changes))


@pytest.mark.asyncio
async def test_new_tax_increases_forecast_expense(ledger):
    baseline=await ForecasterService.create_forecast(ledger,'audit',forecast_request(1))
    await EventService.create_event(ledger,'audit',event())
    updated=await ForecasterService.create_forecast(ledger,'audit',forecast_request(2))
    assert updated.days[0].predicted_expense == baseline.days[0].predicted_expense+40


def test_category_shock_does_not_depend_on_unrelated_expense_document():
    dates=[date(2026,10,2)]
    adjustment=dict(kind='fixed_cost_increase',category_ids=['rent'],percent=20,
                    from_date='2026-10-02',to_date='2026-10-02')
    unrelated=SimpleNamespace(id='suppliers-document',due_date=dates[0],direction='expense',
                              category_id='suppliers',amount=1,paid_amount=0,status='planned')
    without=ScenarioService.apply_scenario_transforms(dates,np.array([100]),np.array([100]),[adjustment])
    with_document=ScenarioService.apply_scenario_transforms(dates,np.array([100]),np.array([100]),[adjustment],[unrelated])
    # Adding a supplier document cannot change the effect of a rent-only shock.
    assert without[1][0] == with_document[1][0]


def test_semimonthly_pattern_keeps_both_payments_in_thirty_days():
    from src.services.forecast_components import compose_flows
    dates=[date(2026,10,2)+timedelta(days=i) for i in range(30)]
    pattern=SimpleNamespace(id='payroll',category_id='payroll',direction='expense',
        counterparty_name='Payroll semi-monthly',cadence='monthly',next_date=dates[0],expected_amount=100)
    _,expense,_,_=compose_flows(dates,np.zeros(30),np.zeros(30),[],[pattern],[])
    assert expense[0]==100
    assert expense[15]==100
    assert expense.sum()==200


@pytest.mark.parametrize('direction',['income','expense'])
def test_two_recurring_components_cannot_replace_each_other(direction):
    from src.services.forecast_components import compose_flows
    patterns=[SimpleNamespace(id=str(i),category_id='rent',direction=direction,
        counterparty_name=str(i),cadence='monthly',next_date=date(2026,10,2),expected_amount=100)
        for i in range(2)]
    income,expense,_,_=compose_flows([date(2026,10,2)],np.zeros(1),np.zeros(1),[],patterns,[])
    assert (income if direction=='income' else expense)[0]==200


@pytest.mark.asyncio
async def test_history_correction_uses_corrected_actual_balance(ledger):
    original=await ForecasterService.create_forecast(ledger,'audit',forecast_request(1))
    actual=await ledger.get(DailyActualRecord,('audit',date(2026,10,1)))
    actual.closing_balance=10
    company=await ledger.get(Company,'audit')
    company.closing_balance=10
    company.data_revision=2
    await ledger.commit()
    result=await IncrementalService.update_forecast(ledger,'audit',original.id,
        ForecastUpdateRequest(changed_from_date='2026-10-01',expected_data_revision=2))
    assert result.forecast.summary.closing_balance == 10


@pytest.mark.asyncio
async def test_event_update_response_preserves_payment_probability(ledger):
    created=await EventService.create_event(ledger,'audit',event())
    request=EventUpdateRequest(**event(on_time_probability=0.7).model_dump(),expected_data_revision=2)
    updated=await EventService.update_event(ledger,'audit',created.id,request)
    assert updated.on_time_probability == 0.7


@pytest.mark.asyncio
async def test_statement_payment_reconciles_outstanding_invoice(ledger):
    invoice=await EventService.create_event(ledger,'audit',event(kind='invoice',direction='income',
        category_id='sales',amount=100,counterparty_name='Client',title='Invoice Audit-1'))
    csv=('date;amount;direction;counterparty_name;counterparty_inn;purpose;closing_balance;external_id\n'
         '2026-10-02;100;income;Client;0012345678;Payment for Invoice Audit-1;200;receipt-1\n')
    mapping=ColumnMapping(date='date',amount='amount',direction='direction',
        counterparty_name='counterparty_name',counterparty_inn='counterparty_inn',
        purpose='purpose',closing_balance='closing_balance',external_id='external_id')
    result=await StatementService.import_file(ledger,'audit',csv.encode(),'csv',mapping)
    assert result.status=='ready'
    rows=await EventService.list_events(ledger,'audit')
    updated=next(item for item in rows.items if item.id==invoice.id)
    assert updated.paid_amount==100
    assert updated.status=='paid'
    assert len(updated.matched_transaction_ids)==1


@pytest.mark.parametrize('changes',[{'amount':-100},{'on_time_probability':2},{'due_date':'2026-02-30'}])
def test_domain_input_rejects_invalid_amount_probability_or_date(changes):
    with pytest.raises(ValidationError):
        event(**changes)


def test_legacy_empty_history_never_fabricates_financial_data():
    with pytest.raises(ValueError):
        FinancialForecaster.forecast_cashflow([],initial_balance=0,horizon_days=60)


@pytest.mark.asyncio
async def test_future_event_update_reuses_model_without_fitting(ledger, monkeypatch):
    original=await ForecasterService.create_forecast(ledger,'audit',forecast_request(1,model='ridge_calendar'))
    await EventService.create_event(ledger,'audit',event())
    def forbidden_fit(*args, **kwargs):
        raise AssertionError('An event-only update must reuse the fitted statistical model')
    monkeypatch.setattr('sklearn.linear_model.Ridge.fit',forbidden_fit)
    updated=await IncrementalService.update_forecast(ledger,'audit',original.id,
        ForecastUpdateRequest(changed_from_date='2026-10-02',expected_data_revision=2))
    assert updated.mode=='incremental'
    assert updated.forecast.days[0].predicted_expense==original.days[0].predicted_expense+40


@pytest.mark.asyncio
async def test_reconciliation_supports_partial_overpayment_and_idempotency(ledger):
    from src.services.payment_matching import reconcile_payments
    from src.models.entities import TransactionRecord
    invoice=await EventService.create_event(ledger,'audit',event(kind='invoice',direction='income',
        category_id='sales',amount=100,counterparty_name='Client',title='Invoice Audit-1'))
    for identifier,amount in [('first',40),('second',80)]:
        ledger.add(TransactionRecord(id=identifier,company_id='audit',date=date(2026,10,2),
            amount=amount,direction='income',counterparty_name='Client',counterparty_inn='00123',
            purpose='Payment for Invoice Audit-1',closing_balance=200,category_id='sales',source_kind='statement'))
        await ledger.flush()
        await reconcile_payments(ledger,'audit')
        await ledger.flush()
        current=(await EventService.list_events(ledger,'audit')).items[0]
        assert current.paid_amount==(40 if identifier=='first' else 120)
        assert current.status==('partial' if identifier=='first' else 'paid')
    assert await reconcile_payments(ledger,'audit') is False


@pytest.mark.asyncio
async def test_receipt_before_invoice_and_late_referenced_receipt_are_reconciled(ledger):
    from src.models.entities import TransactionRecord
    ledger.add(TransactionRecord(id='late',company_id='audit',date=date(2026,11,10),
        amount=100,direction='income',counterparty_name='Client',counterparty_inn='00123',
        purpose='Payment for Invoice Audit-1',closing_balance=200,category_id='sales',source_kind='statement'))
    await ledger.commit()
    invoice=await EventService.create_event(ledger,'audit',event(kind='invoice',direction='income',
        category_id='sales',amount=100,counterparty_name='Client',title='Invoice Audit-1'))
    assert invoice.paid_amount==100
    assert invoice.status=='paid'


@pytest.mark.asyncio
async def test_conflicting_reference_does_not_pay_unrelated_invoice(ledger):
    from src.models.entities import TransactionRecord
    ledger.add(TransactionRecord(id='other',company_id='audit',date=date(2026,10,2),
        amount=100,direction='income',counterparty_name='Client',counterparty_inn='00123',
        purpose='Advance OTHER-200',closing_balance=200,category_id='sales',source_kind='statement'))
    await ledger.commit()
    invoice=await EventService.create_event(ledger,'audit',event(kind='invoice',direction='income',
        category_id='sales',amount=100,counterparty_name='Client',title='Invoice Audit-1'))
    assert invoice.paid_amount==0


@pytest.mark.asyncio
async def test_historical_forecast_does_not_use_a_future_invoice_payment(ledger):
    from src.models.entities import TransactionRecord
    ledger.add(TransactionRecord(id='future',company_id='audit',date=date(2026,10,3),
        amount=100,direction='income',counterparty_name='Client',counterparty_inn='00123',
        purpose='Payment for Invoice Audit-1',closing_balance=200,category_id='sales',source_kind='statement'))
    await ledger.commit()
    invoice=await EventService.create_event(ledger,'audit',event(kind='invoice',direction='income',
        category_id='sales',amount=100,counterparty_name='Client',title='Invoice Audit-1'))
    assert invoice.status=='paid'
    snapshot=await ForecasterService.create_forecast(ledger,'audit',forecast_request(2))
    assert invoice.id in snapshot.days[0].planned_event_ids
    assert snapshot.days[0].predicted_income==200


@pytest.mark.asyncio
async def test_evaluation_uses_runtime_predictions_without_persisting_snapshots(ledger, monkeypatch):
    from src.schemas.contracts import EvaluationRequest
    from src.services.evaluation_service import EvaluationService
    from src.models.entities import ForecastSnapshotRecord
    from sqlalchemy import select, func
    for offset in range(42,100):
        ledger.add(DailyActualRecord(company_id='audit',date=date(2026,10,1)-timedelta(days=offset),
            income=100,expense=100,closing_balance=100,transaction_count=2,observation='observed'))
    await ledger.commit()
    original=ForecasterService.create_forecast
    calls=[]
    async def observed(*args,**kwargs):
        assert kwargs.get('persist') is False
        calls.append(args[2].as_of_date)
        return await original(*args,**kwargs)
    monkeypatch.setattr(ForecasterService,'create_forecast',observed)
    result=await EvaluationService.evaluate_models(ledger,'audit',
        EvaluationRequest(training_end_date='2026-10-01',folds=1,test_days=30))
    assert len(calls)==3
    assert all(cutoff<result.folds[0].test_start for cutoff in calls)
    assert (await ledger.execute(select(func.count()).select_from(ForecastSnapshotRecord))).scalar()==0
    assert all(metric.gap_brier_score==0 for metric in result.metrics)


@pytest.mark.asyncio
async def test_parallel_postgres_import_is_idempotent():
    import asyncio
    import uuid
    from sqlalchemy import delete, select, func
    from src.database import engine
    from src.models.entities import TransactionRecord
    if engine.dialect.name!='postgresql':
        pytest.skip('Concurrent row-lock semantics require PostgreSQL')
    company_id='concurrency-'+uuid.uuid4().hex
    factory=async_sessionmaker(engine,expire_on_commit=False)
    async with factory() as db:
        db.add(Company(id=company_id,name='Isolated concurrency test',business_type='Test',data_revision=0))
        await db.commit()
    mapping=ColumnMapping(date='date',amount='amount',direction='direction',counterparty_name='name',
        counterparty_inn='inn',purpose='purpose',closing_balance='balance',external_id='id')
    payload=b'date;amount;direction;name;inn;purpose;balance;id\n2026-10-02;100;income;Client;00123;Payment;100;receipt-1\n'
    async def upload():
        async with factory() as db:
            return await StatementService.import_file(db,company_id,payload,'csv',mapping)
    try:
        results=await asyncio.gather(upload(),upload())
        assert sorted(result.accepted_rows for result in results)==[0,1]
        assert sorted(result.duplicate_rows for result in results)==[0,1]
        async with factory() as db:
            count=(await db.execute(select(func.count()).select_from(TransactionRecord).where(
                TransactionRecord.company_id==company_id))).scalar()
            assert count==1
            assert (await db.get(Company,company_id)).data_revision==1
    finally:
        async with factory() as db:
            await db.execute(delete(Company).where(Company.id==company_id))
            await db.commit()

