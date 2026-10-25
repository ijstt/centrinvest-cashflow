import io
import json
import uuid
import pytest
import numpy as np
from datetime import date, datetime, timedelta
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.database import async_session_factory
from src.models.entities import Company, PlannedEventRecord, TransactionRecord
from src.schemas.contracts import (
    ColumnMapping, PlannedEventInput, EventUpdateRequest, SourceReference,
    ExternalEventsRequest, InvoiceInput, ContractInput, ContractScheduleItem
)
from src.services.statement_service import StatementService, parse_1c_exchange
from src.services.category_service import CategoryService, clean_vat_clauses
from src.services.recurring_service import RecurringService, add_months
from src.services.event_service import EventService, adjust_tax_due_date_art_6_1
from src.services.scenario_service import ScenarioService

# ==============================================================================
# ATTACK VECTOR 1: 1CClientBankExchange & CSV Parser Vulnerabilities
# ==============================================================================

class TestAttackVector1_IngestionAndParserVulnerabilities:
    def test_attack_1_1_multiline_payment_purpose_truncated_in_1c_exchange(self):
        """
        VULNERABILITY: 1CClientBankExchange parser truncates multiline payment purposes.
        Location: backend/src/services/statement_service.py, lines 116-120.
        
        In 1C exchange format, payment purposes routinely span multiple lines without key=value
        delimiters on subsequent lines. The parser line 117 (`if '=' in line:`) silently discards
        all continuation lines that lack an '=' character. Crucial contract identifiers, invoice
        dates, and VAT clauses are deleted from the ingested record, crippling downstream VAT
        extraction and transaction matching.
        """
        multiline_1c_text = (
            "1CClientBankExchange\n"
            "ВерсияФормата=1.03\n"
            "Кодировка=Windows\n"
            "РасчСчет=40702810000000000001\n"
            "НачальныйОстаток=500000.00\n"
            "КонечныйОстаток=550000.00\n"
            "СекцияДокумент=Платежное поручение\n"
            "Номер=142\n"
            "Дата=20.01.2026\n"
            "Сумма=50000.00\n"
            "ПлательщикСчет=40702810999999999999\n"
            "Плательщик=ООО Покупатель\n"
            "ПлательщикИНН=7701234567\n"
            "ПолучательСчет=40702810000000000001\n"
            "Получатель=ООО НашаКомпания\n"
            "ПолучательИНН=7707111222\n"
            "НазначениеПлатежа=Оплата за строительные материалы\n"
            "по договору поставки № 12/2026 от 10.01.2026\n"
            "В том числе НДС 20% 8333.33 руб.\n"
            "КонецДокумента\n"
            "КонецФайла\n"
        )
        rows, mapping = parse_1c_exchange(multiline_1c_text)
        assert len(rows) == 1
        parsed_purpose = rows[0]["purpose"]
        
        # PROOF OF VULNERABILITY: Continuation lines without '=' are silently dropped
        assert parsed_purpose == "Оплата за строительные материалы"
        assert "договору поставки" not in parsed_purpose
        assert "НДС" not in parsed_purpose

    @pytest.mark.asyncio
    async def test_attack_1_2_missing_external_id_cross_batch_duplicate_suppression(self):
        """
        VULNERABILITY: Content hash occurrence tracking is ephemeral per-file, causing
        false duplicate suppression of valid identical transactions across batches.
        Location: backend/src/services/statement_service.py, lines 346-350 and 435-437.
        
        When statements lack external_id, the service computes a deterministic hash:
        `hash_{content_sig}|occ={occ}`. However, `content_sig_counts` is a local dictionary
        initialized inside `import_file` (line 306). When batch 1 is imported with a valid
        retail check or cash register payment (e.g. 5,000 RUB), it receives `occ=0`.
        When batch 2 arrives later with a second genuine 5,000 RUB payment on the same date,
        `content_sig_counts` resets to 0, producing the exact same hash as batch 1.
        The service treats the second valid payment as a duplicate and silently discards it.
        """
        async with async_session_factory() as db:
            cid = f"test_dup_{uuid.uuid4().hex[:8]}"
            comp = Company(id=cid, name="Retail Store", business_type="Торговля", closing_balance=100000.0, data_revision=0)
            db.add(comp)
            await db.commit()

            mapping = ColumnMapping(
                date="date", amount="amount", direction="direction",
                counterparty_name="counterparty_name", counterparty_inn="counterparty_inn",
                purpose="purpose", closing_balance="closing_balance", external_id=None
            )

            # Batch 1: Cash sale 1
            csv_batch_1 = (
                "date;amount;direction;counterparty_name;counterparty_inn;purpose;closing_balance\n"
                "2026-01-15;5000.00;income;Физическое лицо;;Оплата по чеку ККТ №1;105000.00\n"
            )
            resp1 = await StatementService.import_file(
                db, cid, csv_batch_1.encode("utf-8"), "csv", mapping, balance_policy="use_last_row"
            )
            assert resp1.accepted_rows == 1
            assert resp1.duplicate_rows == 0

            # Batch 2: Cash sale 2 (distinct identical transaction on same date)
            csv_batch_2 = (
                "date;amount;direction;counterparty_name;counterparty_inn;purpose;closing_balance\n"
                "2026-01-15;5000.00;income;Физическое лицо;;Оплата по чеку ККТ №1;110000.00\n"
            )
            resp2 = await StatementService.import_file(
                db, cid, csv_batch_2.encode("utf-8"), "csv", mapping, balance_policy="use_last_row"
            )
            
            # PROOF OF VULNERABILITY: Batch 2 transaction is falsely classified as duplicate
            assert resp2.accepted_rows == 0
            assert resp2.duplicate_rows == 1

    @pytest.mark.asyncio
    async def test_attack_1_3_identical_document_numbers_in_1c_suppresses_distinct_payments(self):
        """
        VULNERABILITY: Naive document number external ID formatting collapses distinct transactions.
        Location: backend/src/services/statement_service.py, line 82.
        
        `parse_1c_exchange` constructs external_id as `f"{d_val}_{doc_num}"`. In Russian banking
        systems, document numbering commonly resets daily or per sub-account, so multiple
        payment orders executed on the same date share document number '1'. Both payments receive
        the identical external ID (`2026-01-15_1`), causing the second distinct supplier payment
        to be permanently discarded as a duplicate.
        """
        sample_1c = (
            "1CClientBankExchange\n"
            "ВерсияФормата=1.03\n"
            "Кодировка=Windows\n"
            "РасчСчет=40702810000000000001\n"
            "НачальныйОстаток=500000.00\n"
            "КонечныйОстаток=300000.00\n"
            "СекцияДокумент=Платежное поручение\n"
            "Номер=1\n"
            "Дата=15.01.2026\n"
            "Сумма=100000.00\n"
            "ПлательщикСчет=40702810000000000001\n"
            "Плательщик=ООО НашаКомпания\n"
            "ПлательщикИНН=7707111222\n"
            "ПолучательСчет=40702810999999999991\n"
            "Получатель=ООО Поставщик Альфа\n"
            "ПолучательИНН=7701111111\n"
            "НазначениеПлатежа=Оплата за металлопрокат\n"
            "КонецДокумента\n"
            "СекцияДокумент=Платежное поручение\n"
            "Номер=1\n"
            "Дата=15.01.2026\n"
            "Сумма=100000.00\n"
            "ПлательщикСчет=40702810000000000001\n"
            "Плательщик=ООО НашаКомпания\n"
            "ПлательщикИНН=7707111222\n"
            "ПолучательСчет=40702810999999999992\n"
            "Получатель=ООО Поставщик Бета\n"
            "ПолучательИНН=7702222222\n"
            "НазначениеПлатежа=Оплата за аренду склада\n"
            "КонецДокумента\n"
            "КонецФайла\n"
        )
        async with async_session_factory() as db:
            cid = f"test_1c_num_{uuid.uuid4().hex[:8]}"
            comp = Company(id=cid, name="Trading Co", business_type="Оптовая торговля", closing_balance=500000.0, data_revision=0)
            db.add(comp)
            await db.commit()

            rows, mapping = parse_1c_exchange(sample_1c)
            # PROOF OF VULNERABILITY: Both separate payments are assigned the identical external_id
            assert rows[0]["external_id"] == "2026-01-15_1"
            assert rows[1]["external_id"] == "2026-01-15_1"

            resp = await StatementService.import_file(
                db, cid, sample_1c.encode("utf-8"), "1c", mapping, balance_policy="use_last_row"
            )
            # Second supplier payment was rejected as duplicate
            assert resp.accepted_rows == 1
            assert resp.duplicate_rows == 1

    @pytest.mark.asyncio
    async def test_attack_1_4_intraday_balance_fluctuation_triggers_conflicting_daily_balance_rejection(self):
        """
        VULNERABILITY: Default balance policy rejects standard statements with intraday balances.
        Location: backend/src/services/statement_service.py, lines 367-377 and 387-404.
        
        Bank statements report the real running balance after each transaction. When multiple
        transactions occur on the same date, their closing balances naturally differ.
        Under the default policy `balance_policy="reject_conflicts"`, the service marks this as
        a critical issue `conflicting_daily_balance` and completely rejects the entire statement.
        """
        csv_intraday = (
            "date;amount;direction;counterparty_name;counterparty_inn;purpose;closing_balance;ext_id\n"
            "2026-01-15;10000.00;income;ООО Покупатель;7701111111;Выручка;110000.00;TX_01\n"
            "2026-01-15;5000.00;expense;ООО Поставщик;7702222222;Расход;105000.00;TX_02\n"
        )
        async with async_session_factory() as db:
            cid = f"test_bal_{uuid.uuid4().hex[:8]}"
            comp = Company(id=cid, name="Balance Corp", business_type="Услуги", data_revision=0)
            db.add(comp)
            await db.commit()

            mapping = ColumnMapping(
                date="date", amount="amount", direction="direction",
                counterparty_name="counterparty_name", counterparty_inn="counterparty_inn",
                purpose="purpose", closing_balance="closing_balance", external_id="ext_id"
            )
            resp = await StatementService.import_file(
                db, cid, csv_intraday.encode("utf-8"), "csv", mapping, balance_policy="reject_conflicts"
            )
            
            # PROOF OF VULNERABILITY: Valid statement is rejected due to normal intraday balance change
            assert resp.status == "rejected"
            assert resp.accepted_rows == 0
            assert resp.rejected_rows == 2
            issue_codes = [iss.code for iss in resp.issues]
            assert "conflicting_daily_balance" in issue_codes

    def test_attack_1_5_csv_empty_leading_line_breaks_delimiter_detection(self):
        """
        VULNERABILITY: Semicolon delimiter detector inspects text.splitlines()[0] without stripping empty lines.
        Location: backend/src/services/statement_service.py, line 244.
        
        If a CSV statement has a leading newline, `text.splitlines()[0]` evaluates to `""`,
        evaluating semicolon count to 0 and falling back to `,`. DictReader then fails to parse
        semicolon columns, mapping the entire line to a single unmapped key.
        """
        csv_leading_newline = (
            "\n"
            "date;amount;direction;counterparty_name;counterparty_inn;purpose;closing_balance\n"
            "2026-01-15;10000.00;income;ООО Покупатель;7701111111;Оплата;100000.00\n"
        )
        text = csv_leading_newline
        header_line = text.splitlines()[0] if text.splitlines() else ""
        semis = header_line.count(";")
        commas = header_line.count(",")
        tabs = header_line.count("\t")
        if tabs > semis and tabs > commas:
            delimiter = "\t"
        elif semis > commas:
            delimiter = ";"
        else:
            delimiter = ","
        
        # PROOF OF VULNERABILITY: Delimiter falls back to comma for semicolon CSV
        assert delimiter == ","


# ==============================================================================
# ATTACK VECTOR 2: Categorization Errors
# ==============================================================================

class TestAttackVector2_CategorizationErrors:
    def test_attack_2_1_clean_vat_clauses_destroys_operational_context_without_punctuation(self):
        r"""
        VULNERABILITY: Over-aggressive regex in clean_vat_clauses deletes trailing operational words.
        Location: backend/src/services/category_service.py, lines 58-71.
        
        The regex pattern `r"[,;\s]*\b(без\s+(налога\s+)?ндс|ндс\s+не\s+облагается)\b[^,;\.]*([,;\.]|$)"`
        and `\b(в\s+т\.?ч\.?|в\s+том\s+числе)\s+ндс\b[^,;\.]*([,;\.]|$)` matches everything after
        the VAT marker up to punctuation or end of string. When payment purpose phrases lack
        intermediate commas, all following domain keywords ('поставка запчастей и ГСМ',
        'оплата за транспортные услуги по доставке товаров') are completely destroyed.
        CategoryService then falls back to 'other_expense' with reduced confidence 0.60.
        """
        purpose_suppliers = "Оплата в т.ч. НДС поставка запчастей и ГСМ"
        cleaned_supp = clean_vat_clauses(purpose_suppliers)
        # PROOF OF VULNERABILITY: Operational keywords are wiped out
        assert cleaned_supp == "Оплата"
        assert "поставка" not in cleaned_supp
        assert "гсм" not in cleaned_supp.lower()

        cat, method, conf = CategoryService.categorize("expense", purpose_suppliers, "ООО АвтоТранс")
        assert cat == "other_expense"
        assert conf == 0.60

        purpose_transport = "Без НДС оплата за транспортные услуги по доставке товаров"
        cleaned_trans = clean_vat_clauses(purpose_transport)
        assert cleaned_trans == ""
        cat_trans, _, conf_trans = CategoryService.categorize("expense", purpose_transport, "ООО Вектор")
        assert cat_trans == "other_expense"
        assert conf_trans == 0.60

    def test_attack_2_2_individual_entrepreneur_12digit_inn_collides_with_systemic_tax_inn(self):
        """
        VULNERABILITY: Prefix matching on systemic tax INN misclassifies 12-digit IP counterparties as taxes.
        Location: backend/src/services/category_service.py, line 126.
        
        The check `clean_inn in (...) or clean_inn.startswith("7727406020")` matches any 12-digit
        Individual Entrepreneur (ИП) INN registered in southwest Moscow tax inspection 7727.
        Commercial payments to an entrepreneur (renting warehouse, purchasing goods) are classified
        as federal tax obligations ('taxes', rule, 0.99) instead of rent or suppliers.
        """
        ip_inn_southwest_moscow = "772740602099"  # 12 digits (valid IP INN format)
        cat_id, method, conf = CategoryService.categorize(
            direction="expense",
            purpose="Оплата по договору аренды складского помещения",
            counterparty_name="ИП Смирнов Алексей Владимирович",
            counterparty_inn=ip_inn_southwest_moscow
        )
        # PROOF OF VULNERABILITY: Misclassified as taxes with 0.99 confidence
        assert cat_id == "taxes"
        assert method == "rule"
        assert conf == 0.99

    def test_attack_2_3_incoming_revenue_with_tax_prefixed_inn_misclassified_as_tax_expense(self):
        """
        VULNERABILITY: Systemic INN check executes before direction validation.
        Location: backend/src/services/category_service.py, line 126.
        
        The INN prefix check at line 126 returns `('taxes', 'rule', 0.99)` immediately,
        regardless of whether `direction` is 'income' or 'expense'. When a business receives
        revenue (`direction == "income"`) from an individual entrepreneur whose INN starts with
        `7727406020`, the revenue is classified into the expense category `taxes`, corrupting
        revenue totals and actuals calculations.
        """
        cat_id, method, conf = CategoryService.categorize(
            direction="income",
            purpose="Оплата за консультационные услуги по договору",
            counterparty_name="ИП Смирнов А.В.",
            counterparty_inn="772740602012"
        )
        # PROOF OF VULNERABILITY: Income transaction categorized as expense category 'taxes'
        assert cat_id == "taxes"
        assert conf == 0.99


# ==============================================================================
# ATTACK VECTOR 3: Calendar Drift in Recurring Event Cadence Calculation
# ==============================================================================

class TestAttackVector3_CalendarDriftInRecurringCadence:
    def test_attack_3_1_add_months_month_end_30th_and_29th_permanently_drifts_to_31st(self):
        """
        VULNERABILITY: add_months permanently mutates 29th and 30th cadences to month-end 31st.
        Location: backend/src/services/recurring_service.py, lines 18-23.
        
        In `add_months`:
            if sourcedate.day == monthrange(sourcedate.year, sourcedate.month)[1]:
                day = max_target
            else:
                day = min(sourcedate.day, max_target)
        When a monthly recurring payment starts on the 30th of January (`2026-01-30`),
        February has 28 days, so `min(30, 28) = 28` (`2026-02-28`).
        On the next monthly step from Feb 28 to March, `sourcedate.day == 28`, which equals
        the end of February! The condition `sourcedate.day == monthrange(...)` evaluates to True,
        permanently setting March to `max_target = 31` (`2026-03-31`).
        In April, 31 becomes 30. In May, 30 becomes 31. The 30th day cadence is permanently lost.
        """
        # Case 1: 30th of month
        jan_30 = date(2026, 1, 30)
        feb_date = add_months(jan_30, 1)
        assert feb_date == date(2026, 2, 28)
        
        mar_date = add_months(feb_date, 1)
        # PROOF OF VULNERABILITY: March drifts to 31 instead of staying on 30
        assert mar_date == date(2026, 3, 31)

        # Case 2: 29th of month
        jan_29 = date(2026, 1, 29)
        feb_29_target = add_months(jan_29, 1)
        assert feb_29_target == date(2026, 2, 28)
        mar_29_target = add_months(feb_29_target, 1)
        # PROOF OF VULNERABILITY: March drifts to 31 instead of staying on 29
        assert mar_29_target == date(2026, 3, 31)

        # Case 3: 28th of month starting in February
        feb_28 = date(2026, 2, 28)
        mar_28_target = add_months(feb_28, 1)
        # PROOF OF VULNERABILITY: 28th payment collapses to month-end 31st in March
        assert mar_28_target == date(2026, 3, 31)

    def test_attack_3_2_tax_due_date_recurring_projection_drifts_from_28th_to_2nd_of_month(self):
        """
        VULNERABILITY: Forecaster recurring projection compounds weekend-adjusted dates,
        drifting monthly tax deadlines by 26 days.
        Location: backend/src/services/forecaster_service.py, lines 301-305.
        
        In `forecaster_service.py`:
            curr_target = add_months(curr_target, 1)
            if ... or pat.category_id == "taxes":
                curr_target = adjust_tax_due_date_art_6_1(curr_target)
        If the current tax target is Saturday 2026-02-28, `adjust_tax_due_date_art_6_1`
        rolls it to Monday 2026-03-02.
        On the next monthly iteration, the code executes `add_months(date(2026, 3, 2), 1)`,
        producing `2026-04-02`! The next iteration produces `2026-05-02`.
        The statutory single tax account deadline (28th of every month under Tax Code Art. 58)
        is permanently shifted into the 2nd day of following months.
        """
        initial_tax = date(2026, 2, 28)  # Saturday
        adjusted_feb = adjust_tax_due_date_art_6_1(initial_tax)
        assert adjusted_feb == date(2026, 3, 2)  # Monday

        # Simulate ForecasterService lines 302-305
        next_month_tax = add_months(adjusted_feb, 1)
        # PROOF OF VULNERABILITY: Shifted to April 2nd instead of April 28th
        assert next_month_tax == date(2026, 4, 2)
        
        following_month_tax = add_months(next_month_tax, 1)
        assert following_month_tax == date(2026, 5, 2)


# ==============================================================================
# ATTACK VECTOR 4: What-If Scenario Flaws
# ==============================================================================

class TestAttackVector4_WhatIfScenarioFlaws:
    def test_attack_4_1_delayed_income_pushed_beyond_horizon_permanently_vanishes(self):
        """
        VULNERABILITY: Delayed income shifted beyond forecast horizon is deducted from
        source day but never accumulated as deferred receivable.
        Location: backend/src/services/scenario_service.py, lines 73-82 and 364.
        
        When a user simulates an adverse scenario where a customer invoice on day 20 is delayed
        by 21 days (horizon 30 days, target date = day 41), the income is subtracted from
        `stressed_income[20]`. Because `target_d_str not in date_to_idx`, the shifted amount is
        not added to any forecast day, not recognized as a deferred receivable, and completely
        vanishes from `total_income`. The business appears to have suffered 100% loss of that revenue.
        """
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(30)]
        base_income = np.full(30, 20000.0)  # 600,000 total
        base_expense = np.full(30, 10000.0)

        class MockEvent:
            id = "ev_delayed_client"
            due_date = dates[19]  # Day 20
            amount = 20000.0
            paid_amount = 0.0
            kind = "invoice"
            direction = "income"
            status = "planned"

        class DelayAdjustment:
            kind = "delay_income"
            event_id = "ev_delayed_client"
            delay_days = 21  # 20 + 21 = 41 (past 30-day horizon)

        stressed_inc, _ = ScenarioService.apply_scenario_transforms(
            future_dates=dates,
            base_income=base_income,
            base_expense=base_expense,
            adjustments=[DelayAdjustment()],
            planned_events=[MockEvent()],
            base_event_ids=[[MockEvent.id] if i == 19 else [] for i in range(30)]
        )

        # PROOF OF VULNERABILITY: 20,000 RUB completely vanished from totals
        assert np.sum(base_income) == 600000.0
        assert np.sum(stressed_inc) == 580000.0
        assert np.sum(base_income) - np.sum(stressed_inc) == 20000.0

    def test_attack_4_2_concurrent_delay_and_revenue_drop_double_penalizes_delayed_sales(self):
        """
        VULNERABILITY: Non-invoice revenue delayed into a revenue drop window is subjected to double haircut.
        Location: backend/src/services/scenario_service.py, lines 78-81 and 110-122.
        
        When a sales payment (`kind == "sales"` or manual receipt) is delayed, `is_fixed_receivable`
        is set to False (line 63 only checks `invoice`, `contract`, `fixed_invoice`).
        At line 79, `delayed_fixed_income_by_target` is NOT populated for this event.
        When the revenue drop adjustment runs on the destination date, the entire delayed amount
        is classified as variable revenue and slashed by the percentage drop, penalizing committed revenue twice.
        """
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(30)]
        base_income = np.zeros(30)
        base_income[2] = 100000.0   # Day 3: 100,000 sales receipt
        base_income[16] = 50000.0   # Day 17: 50,000 normal revenue
        base_expense = np.zeros(30)

        class MockSalesEvent:
            id = "sales_01"
            due_date = dates[2]
            amount = 100000.0
            paid_amount = 0.0
            kind = "sales"
            direction = "income"
            status = "planned"

        class DelayAdjustment:
            kind = "delay_income"
            event_id = "sales_01"
            delay_days = 14  # Shift from Day 3 to Day 17

        class RevenueDropAdjustment:
            kind = "revenue_drop"
            percent = 20.0
            from_date = dates[0]
            to_date = dates[-1]

        stressed_inc, _ = ScenarioService.apply_scenario_transforms(
            future_dates=dates,
            base_income=base_income,
            base_expense=base_expense,
            adjustments=[DelayAdjustment(), RevenueDropAdjustment()],
            planned_events=[MockSalesEvent()],
            base_event_ids=[["sales_01"] if i == 2 else [] for i in range(30)]
        )

        # Expected correct logic: Day 17 should have 50k dropped by 20% (40k) + 100k delayed (100k) = 140,000.
        # PROOF OF VULNERABILITY: All 150,000 on Day 17 is treated as variable and dropped by 20% -> 120,000.
        assert stressed_inc[2] == 0.0
        assert stressed_inc[16] == 120000.0
        assert np.sum(stressed_inc) == 120000.0


# ==============================================================================
# ATTACK VECTOR 5: State Machine and Optimistic Concurrency Failures
# ==============================================================================

class TestAttackVector5_StateMachineAndConcurrencyFailures:
    @pytest.mark.asyncio
    async def test_attack_5_1_missing_unique_constraint_allows_duplicate_planned_events(self):
        """
        VULNERABILITY: Database schema lacks unique constraint on (company_id, source_kind, external_id).
        Location: backend/src/models/entities.py, lines 140-143.
        
        The table `planned_events` defines only a non-unique index:
        `Index("ix_events_company_external", "company_id", "source_kind", "external_id")`.
        Under concurrent batch imports, two requests executing `import_external_events` simultaneously
        pass application-level existence checks before either transaction commits. Both insert duplicate
        invoices and contracts into the database without encountering any database constraint error.
        """
        async with async_session_factory() as db:
            cid = f"test_race_{uuid.uuid4().hex[:8]}"
            comp = Company(id=cid, name="Race Corp", business_type="IT", data_revision=0)
            db.add(comp)
            await db.commit()

            # Insert two identical PlannedEventRecord with the exact same external_id
            ev1 = PlannedEventRecord(
                company_id=cid, title="Счет 101", kind="invoice", direction="income",
                amount=75000.0, due_date=date(2026, 2, 1), category_id="sales",
                source_kind="invoice", external_id="INV_RACE_CONCURRENT"
            )
            ev2 = PlannedEventRecord(
                company_id=cid, title="Счет 101 (копия)", kind="invoice", direction="income",
                amount=75000.0, due_date=date(2026, 2, 1), category_id="sales",
                source_kind="invoice", external_id="INV_RACE_CONCURRENT"
            )
            db.add_all([ev1, ev2])
            with pytest.raises(IntegrityError):
                await db.commit()
            await db.rollback()

    @pytest.mark.asyncio
    async def test_attack_5_2_revision_mismatch_bypass_with_negative_expected_revision(self):
        """
        VULNERABILITY: Optimistic concurrency check in update_event is bypassed when exp_rev < 0 or None.
        Location: backend/src/services/event_service.py, lines 183-189.
        
        `EventService.update_event` contains:
        `if exp_rev is not None and exp_rev >= 0 and exp_rev != comp.data_revision:`
        If a client sends `expected_data_revision = -1` (or omits the field), the optimistic lock
        is completely skipped. The service applies the modification and increments `data_revision`,
        silently overwriting concurrent updates from other users (lost update anomaly).
        """
        async with async_session_factory() as db:
            cid = f"test_rev_bypass_{uuid.uuid4().hex[:8]}"
            comp = Company(id=cid, name="Lock Corp", business_type="Финансы", data_revision=5)
            db.add(comp)
            await db.flush()

            ev = PlannedEventRecord(
                company_id=cid, title="Original Event", kind="manual", direction="expense",
                amount=10000.0, due_date=date(2026, 2, 1), category_id="rent",
                source_kind="manual", status="planned"
            )
            db.add(ev)
            await db.commit()

            from pydantic import ValidationError
            from src.services.event_service import RevisionMismatchError
            values = dict(
                title="Overwritten Event", kind="manual", direction="expense",
                amount=99999.0, due_date="2026-02-01", category_id="rent",
                source=SourceReference(kind="manual", label="Ручной ввод"), cancelled=False,
                expected_data_revision=5
            )
            with pytest.raises(ValidationError):
                EventUpdateRequest(**{**values,'expected_data_revision':-1})
            req = EventUpdateRequest(**values)
            with pytest.raises(RevisionMismatchError):
                await EventService.update_event(db, cid, ev.id, req, expected_data_revision=-1)
            assert ev.title == 'Original Event'
            assert comp.data_revision == 5

    @pytest.mark.asyncio
    async def test_attack_5_3_create_event_lacks_optimistic_revision_check(self):
        """
        VULNERABILITY: create_event lacks expected_data_revision check and blindly mutates revision.
        Location: backend/src/services/event_service.py, lines 108-139.
        
        Unlike `update_event`, `create_event` does not accept or validate `expected_data_revision`.
        When concurrent clients operate with stale view states, `create_event` blindly commits
        and increments `comp.data_revision += 1`, invalidating other concurrent users' revisions
        without any conflict notification.
        """
        async with async_session_factory() as db:
            cid = f"test_create_blind_{uuid.uuid4().hex[:8]}"
            comp = Company(id=cid, name="Blind Corp", business_type="Консалтинг", data_revision=10)
            db.add(comp)
            await db.commit()

            inp = PlannedEventInput(
                title="Unchecked Event", kind="manual", direction="expense",
                amount=15000.0, due_date="2026-03-01", category_id="other_expense",
                source=SourceReference(kind="manual", label="Ручной ввод")
            )
            created = await EventService.create_event(db, cid, inp)
            
            # PROOF OF VULNERABILITY: Event created without revision validation, bumping revision from 10 to 11
            assert created.title == "Unchecked Event"
            assert comp.data_revision == 11

    @pytest.mark.asyncio
    async def test_attack_5_4_contract_schedule_update_silently_ignored(self):
        """
        VULNERABILITY: import_external_events silently ignores modified contract schedule amounts.
        Location: backend/src/services/event_service.py, lines 310-312.
        
        For invoices (lines 261-265), `import_external_events` updates existing amounts if changed.
        However, for contract schedule stages (lines 310-312):
            if key in existing_map:
                duplicate_count += 1
        If a contract amendment alters the stage payment amount (e.g. from 100,000 to 150,000 RUB),
        the update is silently ignored, `duplicate_count` is incremented, and the old amount
        remains stuck in the database, distorting forecast cash flow calculations.
        """
        async with async_session_factory() as db:
            cid = f"test_ctr_update_{uuid.uuid4().hex[:8]}"
            comp = Company(id=cid, name="Contract Corp", business_type="Строительство", data_revision=0)
            db.add(comp)
            await db.commit()

            req1 = ExternalEventsRequest(
                source=SourceReference(kind="contract", source_id="src_1", label="1C Contracts"),
                invoices=[],
                contracts=[
                    ContractInput(
                        external_id="CTR_999",
                        title="Договор генподряда",
                        counterparty_name="ООО Заказчик",
                        direction="income",
                        category_id="sales",
                        signed_date="2026-01-10",
                        payment_terms="Поэтапно",
                        schedule=[ContractScheduleItem(due_date="2026-03-01", amount=100000.0)]
                    )
                ],
                taxes=[]
            )
            resp1 = await EventService.import_external_events(db, cid, req1)
            assert len(resp1.created_events) == 1
            assert resp1.created_events[0].amount == 100000.0

            # Re-import with contract amendment increasing stage amount to 150,000
            req2 = ExternalEventsRequest(
                source=SourceReference(kind="contract", source_id="src_1", label="1C Contracts"),
                invoices=[],
                contracts=[
                    ContractInput(
                        external_id="CTR_999",
                        title="Договор генподряда",
                        counterparty_name="ООО Заказчик",
                        direction="income",
                        category_id="sales",
                        signed_date="2026-01-10",
                        payment_terms="Поэтапно",
                        schedule=[ContractScheduleItem(due_date="2026-03-01", amount=150000.0)]
                    )
                ],
                taxes=[]
            )
            resp2 = await EventService.import_external_events(db, cid, req2)
            
            # PROOF OF VULNERABILITY: Modification ignored as duplicate rather than updated
            assert resp2.updated_count == 0
            assert resp2.duplicate_count == 1
            
            # Verify database still has stale 100,000 amount
            q = await db.execute(
                select(PlannedEventRecord).where(
                    PlannedEventRecord.company_id == cid,
                    PlannedEventRecord.external_id == "CTR_999_stage_1"
                )
            )
            rec = q.scalar_one()
            assert float(rec.amount) == 100000.0
