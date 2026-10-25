import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, Date, DateTime, Text, JSON, Numeric, ForeignKey, Index, UniqueConstraint
)
from src.database import Base

def generate_uuid() -> str:
    return str(uuid.uuid4())

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, default=generate_uuid)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now)

class Session(Base):
    __tablename__ = "sessions"
    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)

class Company(Base):
    __tablename__ = "companies"
    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False)
    business_type = Column(String, nullable=False)
    currency = Column(String, default="RUB")
    timezone = Column(String, default="Europe/Moscow")
    closing_balance = Column(Numeric(18, 2), nullable=True)
    balance_date = Column(Date, nullable=True)
    data_revision = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now)

class CompanyMembership(Base):
    __tablename__ = "company_memberships"
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    company_id = Column(String, ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)

class CategoryOverride(Base):
    __tablename__ = "category_overrides"
    company_id = Column(String, ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    external_id = Column(String, primary_key=True)
    category_id = Column(String, nullable=False)

class ImportRecord(Base):
    __tablename__ = "imports"
    id = Column(String, primary_key=True, default=generate_uuid)
    company_id = Column(String, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String, nullable=False)  # ready, rejected
    accepted_rows = Column(Integer, default=0)
    duplicate_rows = Column(Integer, default=0)
    rejected_rows = Column(Integer, default=0)
    period_start = Column(Date, nullable=True)
    period_end = Column(Date, nullable=True)
    issues = Column(JSON, default=list)
    data_revision = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=utc_now)

class TransactionRecord(Base):
    __tablename__ = "transactions"
    id = Column(String, primary_key=True, default=generate_uuid)
    company_id = Column(String, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    import_id = Column(String, ForeignKey("imports.id", ondelete="SET NULL"), nullable=True)
    date = Column(Date, nullable=False, index=True)
    amount = Column(Numeric(18, 2), nullable=False)
    direction = Column(String, nullable=False)  # income, expense
    counterparty_name = Column(String, nullable=False)
    counterparty_inn = Column(String, default="")
    purpose = Column(Text, default="")
    closing_balance = Column(Numeric(18, 2), nullable=False)
    external_id = Column(String, nullable=True, index=True)
    category_id = Column(String, nullable=False, index=True)
    category_method = Column(String, default="rule")
    category_confidence = Column(Float, nullable=True)
    source_kind = Column(String, default="statement")
    source_id = Column(String, nullable=True)
    source_label = Column(String, default="")
    matched_event_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("ix_transactions_company_date", "company_id", "date"),
        Index("ix_transactions_company_external", "company_id", "external_id"),
    )

class DailyActualRecord(Base):
    __tablename__ = "daily_actuals"
    company_id = Column(String, ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    date = Column(Date, primary_key=True)
    income = Column(Numeric(18, 2), nullable=True)
    expense = Column(Numeric(18, 2), nullable=True)
    closing_balance = Column(Numeric(18, 2), nullable=True)
    transaction_count = Column(Integer, default=0)
    observation = Column(String, default="observed")  # observed, confirmed_no_activity, missing

class RecurringPatternRecord(Base):
    __tablename__ = "recurring_patterns"
    id = Column(String, primary_key=True, default=generate_uuid)
    company_id = Column(String, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    direction = Column(String, nullable=False)
    category_id = Column(String, nullable=False)
    counterparty_name = Column(String, nullable=False)
    cadence = Column(String, nullable=False)  # weekly, monthly, quarterly
    expected_amount = Column(Numeric(18, 2), nullable=False)
    next_date = Column(Date, nullable=False)
    confidence = Column(Float, nullable=False)
    sample_count = Column(Integer, nullable=False)
    transaction_ids = Column(JSON, default=list)

class PlannedEventRecord(Base):
    __tablename__ = "planned_events"
    id = Column(String, primary_key=True, default=generate_uuid)
    company_id = Column(String, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    kind = Column(String, nullable=False)  # manual, invoice, contract, tax, loan, payroll, recurring
    direction = Column(String, nullable=False)  # income, expense
    amount = Column(Numeric(18, 2), nullable=False)
    due_date = Column(Date, nullable=False, index=True)
    category_id = Column(String, nullable=False)
    counterparty_name = Column(String, nullable=True)
    on_time_probability = Column(Float, nullable=True)
    source_kind = Column(String, default="manual")
    source_id = Column(String, nullable=True)
    source_label = Column(String, default="")
    external_id = Column(String, nullable=True, index=True)
    status = Column(String, default="planned")  # planned, partial, paid, overdue, cancelled
    paid_amount = Column(Numeric(18, 2), default=0.0)
    matched_transaction_ids = Column(JSON, default=list)
    recurrence_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    __table_args__ = (
        Index("ix_events_company_due_date", "company_id", "due_date"),
        UniqueConstraint("company_id", "source_kind", "external_id", name="uq_events_company_source_external"),
    )

class ForecastSnapshotRecord(Base):
    __tablename__ = "forecast_snapshots"
    id = Column(String, primary_key=True, default=generate_uuid)
    company_id = Column(String, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    as_of_date = Column(Date, nullable=False)
    horizon_days = Column(Integer, nullable=False)
    currency = Column(String, default="RUB")
    data_revision = Column(Integer, nullable=False)
    model = Column(String, nullable=False)
    model_version = Column(String, default="2.0.0")
    evaluation_id = Column(String, nullable=True)
    probability_method = Column(String, default="residual_block_bootstrap")
    interval_coverage = Column(Float, nullable=True)
    simulation_count = Column(Integer, default=1000)
    random_seed = Column(Integer, nullable=True)
    summary = Column(JSON, nullable=False)
    days = Column(JSON, nullable=False)
    gaps = Column(JSON, nullable=False)
    warnings = Column(JSON, nullable=False)
    data_quality = Column(JSON, nullable=False)
    calculation_state = Column(JSON, nullable=True)

class ScenarioRecord(Base):
    __tablename__ = "scenarios"
    id = Column(String, primary_key=True, default=generate_uuid)
    company_id = Column(String, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    base_forecast_id = Column(String, ForeignKey("forecast_snapshots.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    adjustments = Column(JSON, nullable=False)
    forecast_snapshot = Column(JSON, nullable=False)
    delta = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now)

class EvaluationRecord(Base):
    __tablename__ = "evaluations"
    id = Column(String, primary_key=True, default=generate_uuid)
    company_id = Column(String, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    data_revision = Column(Integer, nullable=False)
    split_method = Column(String, default="rolling_origin")
    test_days = Column(Integer, nullable=False)
    folds = Column(JSON, nullable=False)
    metrics = Column(JSON, nullable=False)
    selected_model = Column(String, nullable=False)
    selection_reason = Column(Text, nullable=False)
    limitations = Column(JSON, default=list)

class IntegrationRecord(Base):
    __tablename__ = "integrations"
    id = Column(String, primary_key=True, default=generate_uuid)
    company_id = Column(String, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String, nullable=False)
    name = Column(String, nullable=False)
    status = Column(String, default="not_configured")  # connected, disconnected, error, not_configured
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    supported_entities = Column(JSON, default=list)
    created_at = Column(DateTime(timezone=True), default=utc_now)
