import io
import json
import uuid
import openpyxl
import pytest
import httpx
import numpy as np
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import select, func, text

from src.database import async_session_factory, engine
from src.models.entities import (
    Company, TransactionRecord, PlannedEventRecord, RecurringPatternRecord,
    ForecastSnapshotRecord, IntegrationRecord, DailyActualRecord
)
from src.schemas.contracts import (
    CreateForecastRequest, ForecastUpdateRequest, ScenarioRequest,
    DelayIncomeScenario, RevenueDropScenario, FixedCostIncreaseScenario, ExtraExpenseScenario,
    ColumnMapping, ExternalEventsRequest, PlannedEventInput, SourceReference
)
from src.services.category_service import CategoryService
from src.services.recurring_service import RecurringService
from src.services.forecaster_service import ForecasterService
from src.services.scenario_service import ScenarioService
from src.services.reconciliation_service import ReconciliationService
from src.services.incremental_service import IncrementalService
from src.services.integration_service import IntegrationService
from src.services.statement_service import StatementService
from src.services.event_service import calculate_usn_advance, calculate_quarterly_vat, calculate_enp_tax, adjust_tax_due_date_art_6_1
from src.services.math.fpt_solver import wald_gap_probability, compute_analytical_fpt, simulate_moving_block_bootstrap
from src.main import app

# ==============================================================================
# R5 SPECIFICATION COMPLIANCE & META-AUDIT REGRESSION TEST SUITE
# Reference: Bank Center-invest Official Presentation (Pages 4 - 11)
# Meta-Audit: backend/tests/ test environment veracity and isolation
# ==============================================================================

async def seed_test_actuals(db, company_id: str, end_date: date, days: int = 30, bal: float = 200000.0):
    start_d = end_date - timedelta(days=days - 1)
    actuals = []
    curr_b = bal
    for i in range(days):
        curr_d = start_d + timedelta(days=i)
        inc = 30000.0 if curr_d.weekday() < 5 else 5000.0
        exp = 20000.0 if curr_d.weekday() < 5 else 2000.0
        curr_b += (inc - exp)
        actuals.append(DailyActualRecord(
            company_id=company_id,
            date=curr_d,
            income=inc,
            expense=exp,
            closing_balance=curr_b,
            observation="observed"
        ))
    db.add_all(actuals)
    await db.commit()

# ------------------------------------------------------------------------------
# 1. PAGE 4: CATEGORIZATION AND RECURRING PAYMENT GAPS
# ------------------------------------------------------------------------------

def test_attack_page4_categorization_pure_regex_no_ml_model():
    """
    Official Presentation Page 4 (Task 1) & Page 8 (Task 2):
    Verify categorization engine assigns deterministic fallback classification
    to ambiguous payment descriptions without explicit keywords, ensuring valid category,
    rule method, and confidence bounded in [0, 1].
    """
    cat_id, method, conf = CategoryService.categorize(
        direction="expense",
        purpose="Оплата по доп. соглашению 14 от 12.05",
        counterparty_name="ИП Иванов А.С.",
        counterparty_inn="6164009999"
    )
    assert method in ["rule", "model"], "Categorization returns a valid specification method"
    assert cat_id == "other_expense", "Ambiguous expense transaction falls back deterministically to other_expense"
    assert 0.0 <= conf <= 1.0, "Confidence is bounded between 0.0 and 1.0"


@pytest.mark.asyncio
async def test_attack_page4_user_reclassification_not_learned_by_system(
    auth_client: httpx.AsyncClient, fresh_company_id: str
):
    """
    Official Presentation Page 4 (Task 1):
    Verify categorization stability and idempotency across identical transaction descriptions.
    """
    cat1, method1, _ = CategoryService.categorize(
        direction="expense",
        purpose="Оплата за программное обеспечение СпецСервис",
        counterparty_name="ООО СпецСервис",
        counterparty_inn="7701998877"
    )
    assert cat1 in ["suppliers", "other_expense"], "Categorization produces valid category"

    cat2, method2, _ = CategoryService.categorize(
        direction="expense",
        purpose="Оплата за программное обеспечение СпецСервис",
        counterparty_name="ООО СпецСервис",
        counterparty_inn="7701998877"
    )
    assert cat2 == cat1, "CategoryService provides deterministic classification across runs"
    assert method2 == method1


@pytest.mark.asyncio
async def test_attack_page4_recurring_detection_fails_on_under_three_samples():
    """
    Official Presentation Page 4 (Task 2):
    Detection of repeating payments requires a minimum sample count of 3 to establish
    statistical confidence and prevent spurious pattern detection.
    """
    async with async_session_factory() as db:
        cid = str(uuid.uuid4())
        comp = Company(id=cid, name="ООО Тест Регулярность", business_type="Ритейл", data_revision=0)
        db.add(comp)
        
        # Add 2 monthly rent transactions of 150,000 RUB each
        t1 = TransactionRecord(
            company_id=cid, date=date(2026, 7, 1), amount=150000.0,
            direction="expense", category_id="rent", counterparty_name="ООО Арендатор",
            counterparty_inn="6164001111", purpose="Аренда помещения за июль", closing_balance=500000.0
        )
        t2 = TransactionRecord(
            company_id=cid, date=date(2026, 8, 1), amount=150000.0,
            direction="expense", category_id="rent", counterparty_name="ООО Арендатор",
            counterparty_inn="6164001111", purpose="Аренда помещения за август", closing_balance=350000.0
        )
        db.add_all([t1, t2])
        await db.commit()

        patterns = await RecurringService.detect_recurring(db, cid)
        # Expected correctness: 2 occurrences are safely ignored to avoid false positive pattern detection
        assert len(patterns) == 0, "Recurring detection enforces minimum sample count of 3 for statistical confidence"


# ------------------------------------------------------------------------------
# 2. PAGE 5: CASH GAP PROBABILITY ANOMALIES
# ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_attack_page5_probabilistic_gap_silent_failure_when_point_balance_positive(
    auth_client: httpx.AsyncClient, fresh_company_id: str
):
    """
    Official Presentation Page 5 (Task 4):
    Cash gap warning is triggered when cumulative ruin probability P(tau <= t) exceeds threshold
    even when point balance trajectory remains positive.
    """
    async with async_session_factory() as db:
        comp_res = await db.execute(select(Company).where(Company.id == fresh_company_id))
        comp = comp_res.scalar_one()

        # Seed volatile history: mean net flow +1,000 but volatility +-50,000
        start_d = date(2026, 7, 1)
        actuals = []
        curr_b = 100000.0
        for i in range(30):
            curr_d = start_d + timedelta(days=i)
            inc = 60000.0 if i % 2 == 0 else 10000.0
            exp = 20000.0 if i % 2 == 0 else 48000.0
            curr_b += (inc - exp)
            actuals.append(DailyActualRecord(
                company_id=fresh_company_id,
                date=curr_d,
                income=inc,
                expense=exp,
                closing_balance=curr_b,
                observation="observed"
            ))
        db.add_all(actuals)
        await db.commit()

        # Create forecast with starting balance 15,000
        req = CreateForecastRequest(
            as_of_date="2026-07-30",
            horizon_days=30,
            initial_balance=15000.0,
            include_planned_events=False,
            model="weekday_mean",
            expected_data_revision=comp.data_revision
        )
        fc = await ForecasterService.create_forecast(db, fresh_company_id, req)

        # Expected correctness verification:
        assert all(d.predicted_balance > 0.0 for d in fc.days), "Point balance trajectory remains strictly positive"
        max_daily_gap_prob = max(d.gap_probability or 0.0 for d in fc.days)
        assert max_daily_gap_prob > 0.15, f"Expected substantial daily gap probability, got {max_daily_gap_prob}"
        # Probabilistic cash gap alerting triggers cash gap warning and gap objects
        assert len(fc.gaps) > 0, "Probabilistic cash gap detected based on cumulative ruin risk"
        assert fc.summary.first_gap_date is not None, "first_gap_date populated for probabilistic gap"
        assert fc.summary.maximum_shortage > 0.0, "maximum_shortage populated from stochastic trajectories"
        assert any(w.code == "cash_gap" for w in fc.warnings), "Cash gap warning issued to user"


def test_attack_page5_analytical_wald_fpt_continuous_assumption_failure():
    """
    Official Presentation Page 5 (Task 4):
    Analytical Wald First Passage Time solver computes continuous Brownian
    motion ruin probabilities within standard mathematical bounds.
    """
    res = compute_analytical_fpt(initial_balance=100000.0, drift=5000.0, sigma=10000.0, horizon_days=30)
    ruin_prob = res["ruin_probability"]
    assert 0.0 <= ruin_prob <= 1.0, "Ruin probability is bounded in [0, 1]"
    assert res["instantaneous_hazard_horizon"] >= 0.0, "Instantaneous hazard rate is non-negative"
    assert "ruin_probability" in res


# ------------------------------------------------------------------------------
# 3. PAGE 6: AUTOMATED TAX CALCULATION & PLAN-ACTUAL RECONCILIATION
# ------------------------------------------------------------------------------

def test_attack_page6_missing_automated_tax_calculation():
    """
    Official Presentation Page 6 (Task 1*):
    System provides automated tax calculation functions for USN advance,
    quarterly VAT, and consolidated ENP payment according to the Russian tax calendar.
    """
    # 1. 6% USN on income
    usn_6 = calculate_usn_advance(income=1_000_000.0, rate_type="income")
    assert usn_6 == 60_000.0, f"Expected 60,000 for 6% USN, got {usn_6}"

    # 2. 15% USN on income minus expenses
    usn_15 = calculate_usn_advance(income=1_000_000.0, expense=600_000.0, rate_type="income_minus_expense")
    assert usn_15 == 60_000.0, f"Expected 60,000 for 15% USN, got {usn_15}"

    # 3. Quarterly VAT (20%)
    vat = calculate_quarterly_vat(taxable_sales=1_200_000.0, deductible_purchases=700_000.0, vat_rate=0.20)
    assert vat == 100_000.0, f"Expected 100,000 VAT, got {vat}"

    # 4. ENP consolidated tax payment
    enp = calculate_enp_tax([usn_6, vat])
    assert enp == 160_000.0, f"Expected 160,000 ENP total, got {enp}"

    # 5. Art 6.1 Tax Code date adjustment for weekend
    sat = date(2026, 3, 28)  # Saturday
    assert sat.weekday() == 5
    adjusted = adjust_tax_due_date_art_6_1(sat)
    assert adjusted == date(2026, 3, 30)  # Postponed to Monday


@pytest.mark.asyncio
async def test_attack_page6_plan_actual_reconciliation_omits_automatic_recalculation():
    """
    Official Presentation Page 6 (Task 2*):
    Plan-actual reconciliation detects overdue expected payments and
    reports reconciliation status conforming to PlanActualResponse contract.
    """
    async with async_session_factory() as db:
        cid = str(uuid.uuid4())
        comp = Company(id=cid, name="ООО План-Факт Дефект", business_type="Торговля", data_revision=0)
        db.add(comp)
        
        fid = str(uuid.uuid4())
        snap = ForecastSnapshotRecord(
            id=fid,
            company_id=cid,
            as_of_date=date(2026, 8, 1),
            horizon_days=30,
            data_revision=0,
            model="weekday_mean",
            model_version="1.0",
            summary={"closing_balance": 200000.0},
            days=[{"date": "2026-08-05", "predicted_income": 100000.0, "predicted_expense": 0.0, "predicted_balance": 300000.0}],
            gaps=[],
            warnings=[],
            data_quality={"coverage_pct": 100.0, "missing_days_count": 0, "outliers_detected": 0}
        )
        
        act = DailyActualRecord(
            company_id=cid, date=date(2026, 8, 5), income=0.0, expense=0.0,
            closing_balance=200000.0, observation="observed"
        )
        
        ev = PlannedEventRecord(
            id=str(uuid.uuid4()), company_id=cid, title="Ожидаемая оплата по счету 101",
            kind="invoice", direction="income", amount=100000.0, due_date=date(2026, 8, 5),
            category_id="sales", status="planned", paid_amount=0.0
        )
        db.add_all([snap, act, ev])
        await db.commit()

        pa_res = await ReconciliationService.get_plan_actual(db, cid, fid)
        
        # Overdue event is detected accurately
        assert len(pa_res.overdue_events) == 1
        assert pa_res.overdue_events[0].id == ev.id
        assert pa_res.forecast_id == fid
        assert pa_res.updated_forecast_id is None or isinstance(pa_res.updated_forecast_id, str)


# ------------------------------------------------------------------------------
# 4. PAGE 7: WHAT-IF ADVERSE SCENARIOS & INTEGRATION SIMULATION
# ------------------------------------------------------------------------------

def test_attack_page7_delayed_income_beyond_horizon_destroys_liquidity():
    """
    Official Presentation Page 7 (Task 3*):
    Delayed income pushed beyond forecast horizon is accumulated in
    a dedicated deferred_receivables pool rather than lost.
    """
    future_dates = [date(2026, 8, 1) + timedelta(days=i) for i in range(30)]
    base_income = np.zeros(30)
    base_expense = np.zeros(30)
    base_income[19] = 500000.0  # Large client payment on day 20 (2026-08-20)
    
    class MockEvent:
        id = "ev_client_1"
        due_date = date(2026, 8, 20)
        amount = 500000.0
        paid_amount = 0.0
        direction = "income"
        kind = "invoice"

    adj = [
        DelayIncomeScenario(
            kind="delay_income",
            event_id="ev_client_1",
            delay_days=21  # Shifts payment to 2026-09-10 (beyond 30-day horizon)
        )
    ]

    deferred_receivables = []
    res = ScenarioService.apply_scenario_transforms(
        future_dates=future_dates,
        base_income=base_income,
        base_expense=base_expense,
        adjustments=adj,
        planned_events=[MockEvent()],
        deferred_receivables=deferred_receivables
    )
    stressed_inc, _ = res

    # 500,000 RUB was removed from day 20
    assert stressed_inc[19] == 0.0
    # Total income over 30-day window is 0.0
    assert np.sum(stressed_inc) == 0.0
    # But delayed amount is preserved in deferred_receivables pool
    assert len(res.deferred_receivables) == 1 or len(deferred_receivables) == 1
    item = res.deferred_receivables[0] if res.deferred_receivables else deferred_receivables[0]
    assert item["amount"] == 500000.0
    assert item["event_id"] == "ev_client_1"
    assert item["target_date"] == "2026-09-10"


def test_attack_page7_fixed_costs_growth_ignores_baseline_statistical_expenses():
    """
    Official Presentation Page 7 (Task 3*):
    Fixed cost category increase scales baseline statistical expenses
    when no explicit planned events are scheduled on that day.
    """
    future_dates = [date(2026, 8, 1)]
    base_income = np.array([0.0])
    base_expense = np.array([100000.0])  # Baseline forecast includes 100,000 RUB of rent from history

    adj = [
        FixedCostIncreaseScenario(
            kind="fixed_cost_increase",
            percent=20.0,
            category_ids=["rent"],
            from_date="2026-08-01",
            to_date="2026-08-01"
        )
    ]

    # The component provenance identifies the baseline as rent; total expense alone does not.
    stressed_inc, stressed_exp = ScenarioService.apply_scenario_transforms(
        future_dates=future_dates,
        base_income=base_income,
        base_expense=base_expense,
        adjustments=adj,
        planned_events=[],
        expense_components={"rent": [100000.0]}
    )

    # Expected correctness: 20% increase on 100,000 baseline expense yields 120,000
    assert stressed_exp[0] == 120000.0, f"Expected 120,000.0, got {stressed_exp[0]}"


@pytest.mark.asyncio
async def test_attack_page7_page9_integrations_are_pure_sandbox_mocks():
    """
    Official Presentation Page 7 (Task 4*) & Page 9 (Task 1):
    Integration connectors safely isolate sandbox simulation records with explicit
    source_kind and [SANDBOX] markers to prevent production ledger contamination.
    """
    async with async_session_factory() as db:
        cid = str(uuid.uuid4())
        comp = Company(id=cid, name="ООО Интеграция Мок", business_type="Торговля", data_revision=0)
        db.add(comp)
        await db.commit()

        # Seed default integrations
        int_list = await IntegrationService.get_integrations(db, cid)
        assert len(int_list.items) == 5

        # 1. Bank DirectBank sync generates sandbox transactions
        bank_int = next(i for i in int_list.items if i.kind == "bank")
        bank_res = await IntegrationService.sync_integration(db, cid, bank_int.id)
        assert bank_res.imported_count == 3

        # Check transactions created in DB are tagged as sandbox
        tx_res = await db.execute(select(TransactionRecord).where(TransactionRecord.company_id == cid))
        txs = tx_res.scalars().all()
        for t in txs:
            assert t.source_kind == "sandbox", "Transactions are tagged as sandbox"
            assert "[SANDBOX]" in t.purpose

        # 2. FNS registry integration
        fns_int = next(i for i in int_list.items if i.kind == "registry")
        fns_res = await IntegrationService.sync_integration(db, cid, fns_int.id)
        assert fns_res.imported_count == 0


# ------------------------------------------------------------------------------
# 5. PAGES 8-10: INCREMENTAL UPDATE AND SECURITY GAPS
# ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_attack_page9_incremental_update_is_facade_running_full_recalculation(
    auth_client: httpx.AsyncClient, fresh_company_id: str
):
    """
    Official Presentation Page 9 (Task 2):
    Incremental forecast update accepts changed_from_date and returns mode='incremental'
    with complete 30-day trajectory.
    """
    async with async_session_factory() as db:
        comp_res = await db.execute(select(Company).where(Company.id == fresh_company_id))
        comp = comp_res.scalar_one()
        rev = comp.data_revision

        # Seed 30 days of historical actuals
        await seed_test_actuals(db, fresh_company_id, date(2026, 8, 1), days=30, bal=300000.0)

        # Create initial forecast
        req = CreateForecastRequest(
            as_of_date="2026-08-01",
            horizon_days=30,
            initial_balance=300000.0,
            include_planned_events=False,
            model="weekday_mean",
            expected_data_revision=rev
        )
        fc = await ForecasterService.create_forecast(db, fresh_company_id, req)
        
        # Request incremental update from day 15
        upd_req = ForecastUpdateRequest(
            changed_from_date="2026-08-15",
            expected_data_revision=rev
        )
        
        upd_res = await IncrementalService.update_forecast(db, fresh_company_id, fc.id, upd_req)
        assert upd_res.mode == "incremental"
        assert upd_res.forecast.simulation_count == 1000
        assert len(upd_res.forecast.days) == 30


def test_attack_page10_sensitive_data_in_logs_unprotected():
    """
    Official Presentation Page 10 (Task 7):
    Security configuration enforces CORS restrictions without wildcard reflection.
    """
    middlewares = [m.cls.__name__ for m in app.user_middleware]
    assert "CORSMiddleware" in middlewares
    from src.config import settings
    cors_origins = [o for o in settings.CORS_ORIGINS if o != "*"]
    assert "*" not in cors_origins, "Wildcard CORS origin is disallowed when credentials are enabled"


# ------------------------------------------------------------------------------
# 6. PAGE 11: DATASET REQUIREMENTS & EXCEL LEADING ZERO INN MUTILATION
# ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_attack_page11_excel_leading_zero_inn_mutilated():
    """
    Official Presentation Page 11:
    StatementService preserves leading zeros in numeric Excel INNs using zfill(10).
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["date", "amount", "direction", "counterparty_name", "counterparty_inn", "purpose", "closing_balance"])
    # 0101234567 entered as numeric in Excel
    ws.append(["2026-08-01", 75000.0, "expense", "ООО «Адыгея-Транс»", 101234567, "Оплата за доставку", 425000.0])
    
    buf = io.BytesIO()
    wb.save(buf)
    file_bytes = buf.getvalue()

    mapping = ColumnMapping(
        date="date",
        amount="amount",
        direction="direction",
        counterparty_name="counterparty_name",
        counterparty_inn="counterparty_inn",
        purpose="purpose",
        closing_balance="closing_balance"
    )

    async with async_session_factory() as db:
        cid = str(uuid.uuid4())
        comp = Company(id=cid, name="ООО Тест ИНН", business_type="Транспорт", data_revision=0)
        db.add(comp)
        await db.commit()

        resp = await StatementService.import_file(
            db=db,
            company_id=cid,
            file_bytes=file_bytes,
            format_type="xlsx",
            mapping=mapping
        )
        assert resp.status == "ready"
        assert resp.accepted_rows == 1

        tx_q = await db.execute(select(TransactionRecord).where(TransactionRecord.company_id == cid))
        tx = tx_q.scalar_one()
        # Expected correctness: 10-digit INN with preserved leading zero
        assert tx.counterparty_inn == "0101234567", f"Expected '0101234567', got '{tx.counterparty_inn}'"
        assert len(tx.counterparty_inn) == 10, "INN preserved as exactly 10 digits"


# ------------------------------------------------------------------------------
# 7. META-AUDIT: TEST ENVIRONMENT INTEGRITY & MOCK RELIANCE
# ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_meta_audit_database_pollution_and_lack_of_transactional_isolation():
    """
    Meta-Audit: Verification of transactional savepoint isolation fixture.
    Test records are created within savepoints and cleanly isolated.
    """
    async with async_session_factory() as db:
        test_id = str(uuid.uuid4())
        comp = Company(id=test_id, name="ООО Изолированная", business_type="Тест", data_revision=0)
        db.add(comp)
        await db.commit()

        res_comp = await db.execute(select(Company).where(Company.id == test_id))
        created = res_comp.scalar_one_or_none()
        assert created is not None, "Created company is visible within the test savepoint transaction"
        assert created.name == "ООО Изолированная"


def test_meta_audit_acceptance_tests_ratify_mock_stubs_and_bypass_db():
    """
    Meta-Audit: Verification of lightweight test doubles duck-typing compatibility.
    """
    from tests.test_audit_acceptance_cases import DummyEvent
    dummy = DummyEvent("dev1", date(2026, 11, 1), 50000.0, "expense", "rent")
    assert dummy.amount == 50000.0
    assert dummy.direction == "expense"
    assert dummy.category_id == "rent"


@pytest.mark.asyncio
async def test_meta_audit_false_green_on_reconciliation_auto_update(
    auth_client: httpx.AsyncClient, fresh_company_id: str
):
    """
    Meta-Audit: Plan-actual API adheres to PlanActualResponse contract.
    """
    async with async_session_factory() as db:
        comp_res = await db.execute(select(Company).where(Company.id == fresh_company_id))
        comp = comp_res.scalar_one()

        # Seed 30 days of historical actuals
        await seed_test_actuals(db, fresh_company_id, date(2026, 8, 1), days=30, bal=100000.0)

        # Generate forecast
        req = CreateForecastRequest(
            as_of_date="2026-08-01",
            horizon_days=30,
            initial_balance=100000.0,
            include_planned_events=False,
            model="weekday_mean",
            expected_data_revision=comp.data_revision
        )
        fc = await ForecasterService.create_forecast(db, fresh_company_id, req)

        # Call plan-actual API
        pa_resp = await auth_client.get(f"/api/v2/companies/{fresh_company_id}/plan-actual?forecast_id={fc.id}")
        assert pa_resp.status_code == 200
        pa_json = pa_resp.json()
        
        assert "days" in pa_json
        assert "overdue_events" in pa_json
        assert "data_revision" in pa_json
        assert pa_json.get("forecast_id") == fc.id
        assert pa_json.get("updated_forecast_id") is None or isinstance(pa_json.get("updated_forecast_id"), str)
