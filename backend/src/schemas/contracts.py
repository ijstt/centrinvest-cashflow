from typing import Literal, Annotated, Union
from datetime import date
from pydantic import BaseModel, Field, ConfigDict, AfterValidator
from pydantic.alias_generators import to_camel

def validate_local_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('Expected a calendar date in YYYY-MM-DD format')
    return value

LocalDate = Annotated[str, AfterValidator(validate_local_date)]
UtcTimestamp = str
EntityId = str
Money = Annotated[float, Field(allow_inf_nan=False)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Direction = Literal["income", "expense"]
RiskLevel = Literal["low", "medium", "critical"]
Currency = Literal["RUB"]

# v1 interfaces
class TransactionPoint(BaseModel):
    model_config = ConfigDict(extra="ignore")
    date: LocalDate
    income: float
    expense: float

class ForecastRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)
    initial_balance: Money = 150000.0
    horizon_days: int = Field(default=14, ge=1, le=90)
    history: list[TransactionPoint] = Field(min_length=1)

class ForecastResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    predicted_dates: list[LocalDate]
    predicted_income: list[Money]
    predicted_expense: list[Money]
    predicted_balance: list[Money]
    cashflow_gap_detected: bool
    gap_dates: list[LocalDate]
    minimum_expected_balance: Money
    risk_level: RiskLevel

class AdvisorRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    forecast: ForecastResult
    business_type: str = "Розничная торговля / Кофейня"

class AdvisorResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    analysis: str
    recommendations: list[str]
    urgent_action_required: bool

class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str
    app: str

class ApiErrorDetail(BaseModel):
    field: str
    message: str

class ApiError(BaseModel):
    model_config = ConfigDict(extra="ignore")
    code: Literal[
        "validation_error", "unauthorized", "forbidden", "not_found", "conflict",
        "insufficient_data", "too_large", "rate_limited", "unavailable", "internal_error"
    ]
    message: str
    request_id: str
    details: list[ApiErrorDetail] = []

class PageQuery(BaseModel):
    limit: int = 50
    cursor: str | None = None

class DateRangeQuery(BaseModel):
    from_date: LocalDate | None = None
    to_date: LocalDate | None = None

class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    email: str
    password: str

class UserSessionInfo(BaseModel):
    id: EntityId
    display_name: str
    company_ids: list[EntityId]

class SessionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user: UserSessionInfo
    expires_at: UtcTimestamp

class LogoutResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    signed_out: bool

FeatureName = Literal[
    "imports", "categorization", "recurring", "forecast_v2", "probability",
    "events", "scenarios", "plan_actual", "evaluation", "integrations", "advice"
]

class CapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    contract_version: str = "2.0.0"
    features: list[FeatureName]

class Company(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: EntityId
    name: str
    business_type: str
    currency: Currency = "RUB"
    timezone: str = "Europe/Moscow"
    closing_balance: Money | None = None
    balance_date: LocalDate | None = None
    data_revision: int = 0

class CompanyListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[Company]

class CompanyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    business_type: str
    timezone: str = "Europe/Moscow"

CategoryId = Literal[
    "sales", "other_income", "payroll", "rent", "suppliers",
    "taxes", "loan", "fees", "equipment", "transfer", "other_expense", "uncategorized"
]

class Category(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: CategoryId
    name: str
    direction: Literal["income", "expense", "both"]

class CategoryListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[Category]

SourceKind = Literal[
    "statement", "manual", "invoice", "contract", "tax",
    "bank", "accounting", "cash_register", "marketplace", "registry"
]

class SourceReference(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: SourceKind
    source_id: EntityId | None = None
    external_id: str | None = None
    label: str

class StatementOperationInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    date: LocalDate
    amount: Money
    direction: Direction
    counterparty_name: str
    counterparty_inn: str
    purpose: str
    closing_balance: Money
    external_id: str | None = None

class Transaction(StatementOperationInput):
    id: EntityId
    company_id: EntityId
    category_id: CategoryId
    category_method: Literal["rule", "model", "manual", "unknown"]
    category_confidence: Probability | None = None
    source: SourceReference
    matched_event_id: EntityId | None = None

class TransactionQuery(PageQuery, DateRangeQuery):
    direction: Direction | None = None
    category_id: CategoryId | None = None
    search: str | None = None

class TransactionListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[Transaction]
    next_cursor: str | None = None
    total: int

class CategoryUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    category_id: CategoryId

class DailyActual(BaseModel):
    model_config = ConfigDict(extra="ignore")
    date: LocalDate
    income: Money | None = None
    expense: Money | None = None
    closing_balance: Money | None = None
    transaction_count: int
    observation: Literal["observed", "confirmed_no_activity", "missing"]

class CategoryTotal(BaseModel):
    model_config = ConfigDict(extra="ignore")
    category_id: CategoryId
    direction: Direction
    amount: Money
    count: int

class ActualsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    company_id: EntityId
    data_revision: int
    days: list[DailyActual]
    category_totals: list[CategoryTotal]

class ActualsQuery(DateRangeQuery):
    granularity: Literal["day", "week"] | None = "day"

class ColumnMapping(BaseModel):
    model_config = ConfigDict(extra="ignore")
    date: str = "operation_date"
    amount: str = "amount"
    direction: str = "direction"
    counterparty_name: str = "counterparty_name"
    counterparty_inn: str = "counterparty_inn"
    purpose: str = "payment_purpose"
    closing_balance: str = "balance_eod"
    external_id: str | None = "operation_id"

class ImportIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")
    row: int | None = None
    field: str | None = None
    code: str
    message: str

class ImportResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: EntityId
    company_id: EntityId
    status: Literal["ready", "rejected"]
    accepted_rows: int
    duplicate_rows: int
    rejected_rows: int
    period_start: LocalDate | None = None
    period_end: LocalDate | None = None
    issues: list[ImportIssue] = []
    data_revision: int

class RecurringPattern(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: EntityId
    direction: Direction
    category_id: CategoryId
    counterparty_name: str
    cadence: Literal["weekly", "monthly", "quarterly"]
    expected_amount: Money
    next_date: LocalDate
    confidence: Probability
    sample_count: int
    transaction_ids: list[EntityId]

class RecurringListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[RecurringPattern]

EventKind = Literal["manual", "invoice", "contract", "tax", "loan", "payroll", "recurring"]
EventStatus = Literal["planned", "partial", "paid", "overdue", "cancelled"]

class PlannedEventInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str
    kind: EventKind
    direction: Direction
    amount: Money = Field(gt=0)
    due_date: LocalDate
    category_id: CategoryId
    counterparty_name: str | None = None
    on_time_probability: Probability | None = None
    source: SourceReference

class PlannedEvent(PlannedEventInput):
    id: EntityId
    company_id: EntityId
    status: EventStatus
    paid_amount: Money = 0.0
    matched_transaction_ids: list[EntityId] = []
    recurrence_id: EntityId | None = None
    updated_at: UtcTimestamp

class EventQuery(PageQuery, DateRangeQuery):
    status: EventStatus | None = None
    kind: EventKind | None = None

class EventListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[PlannedEvent]
    next_cursor: str | None = None

class EventUpdateRequest(PlannedEventInput):
    cancelled: bool = False
    expected_data_revision: int | None = Field(default=None, ge=0)

class InvoiceInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    external_id: str
    number: str
    issued_date: LocalDate
    due_date: LocalDate
    amount: Money = Field(gt=0)
    direction: Direction
    counterparty_name: str
    category_id: CategoryId
    on_time_probability: Probability | None = None

class ContractScheduleItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    due_date: LocalDate
    amount: Money = Field(gt=0)
    on_time_probability: Probability | None = None

class ContractInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    external_id: str
    title: str
    counterparty_name: str
    direction: Direction
    category_id: CategoryId
    signed_date: LocalDate
    payment_terms: str
    schedule: list[ContractScheduleItem]

class TaxPaymentInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    external_id: str
    title: str
    due_date: LocalDate
    amount: Money = Field(gt=0)
    period: str
    authority_source_url: str | None = None
    confirmed_by_user: bool = True

class ExternalEventsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    source: SourceReference
    invoices: list[InvoiceInput] = []
    contracts: list[ContractInput] = []
    taxes: list[TaxPaymentInput] = []

class ExternalEventsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    created_events: list[PlannedEvent]
    updated_count: int
    duplicate_count: int
    data_revision: int

ModelId = Literal["seasonal_naive", "weekday_mean", "ridge_calendar"]

class CreateForecastRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)
    as_of_date: LocalDate
    horizon_days: Literal[30, 60, 90]
    initial_balance: Money | None = None
    include_planned_events: bool = True
    model: Literal["seasonal_naive", "weekday_mean", "ridge_calendar", "auto"] = "auto"
    expected_data_revision: int = Field(default=0, ge=0)

class ForecastDay(BaseModel):
    model_config = ConfigDict(extra="ignore")
    date: LocalDate
    predicted_income: Money
    predicted_expense: Money
    predicted_balance: Money
    balance_lower: Money | None = None
    balance_upper: Money | None = None
    gap_probability: Probability | None = None
    planned_event_ids: list[EntityId] = []

class CashGap(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start_date: LocalDate
    end_date: LocalDate
    minimum_balance_date: LocalDate
    shortage_amount: Money
    probability: Probability | None = None
    contributing_event_ids: list[EntityId] = []
    explanation: str

WarningCode = Literal[
    "cash_gap", "low_balance", "overdue_income", "missing_data", "short_history",
    "unreconciled_balance", "uncertain_probability", "outlier"
]

class ForecastWarning(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: EntityId
    code: WarningCode
    severity: Literal["info", "warning", "critical"]
    title: str
    message: str
    date: LocalDate | None = None
    amount: Money | None = None
    event_id: EntityId | None = None

class ForecastSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    closing_balance: Money
    end_balance: Money
    total_income: Money
    total_expense: Money
    minimum_balance: Money
    minimum_balance_date: LocalDate
    first_gap_date: LocalDate | None = None
    maximum_shortage: Money
    any_gap_probability: Probability | None = None
    risk_level: RiskLevel

class DataQuality(BaseModel):
    model_config = ConfigDict(extra="ignore")
    observed_days: int
    missing_days: int
    duplicate_rows: int
    uncategorized_share: Probability
    warnings: list[str] = []

class ForecastSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: EntityId
    company_id: EntityId
    created_at: UtcTimestamp
    as_of_date: LocalDate
    horizon_days: Literal[30, 60, 90]
    currency: Currency = "RUB"
    data_revision: int
    model: ModelId
    model_version: str = "2.0.0"
    evaluation_id: EntityId | None = None
    probability_method: Literal["residual_block_bootstrap", "unavailable"]
    interval_coverage: Probability | None = None
    simulation_count: int = 1000
    random_seed: int | None = 42
    summary: ForecastSummary
    days: list[ForecastDay]
    gaps: list[CashGap]
    warnings: list[ForecastWarning]
    data_quality: DataQuality

class ForecastUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)
    changed_from_date: LocalDate
    expected_data_revision: int = Field(ge=0)

class ForecastUpdateResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    forecast: ForecastSnapshot
    mode: Literal["incremental", "full_retrain"]
    recomputed_from_date: LocalDate
    reason: str

class DelayIncomeScenario(BaseModel):
    kind: Literal["delay_income"] = "delay_income"
    event_id: EntityId
    delay_days: int

class RevenueDropScenario(BaseModel):
    kind: Literal["revenue_drop"] = "revenue_drop"
    percent: float
    from_date: LocalDate
    to_date: LocalDate

class ExtraExpenseScenario(BaseModel):
    kind: Literal["extra_expense"] = "extra_expense"
    amount: Money = Field(gt=0)
    date: LocalDate
    category_id: CategoryId

class FixedCostIncreaseScenario(BaseModel):
    kind: Literal["fixed_cost_increase"] = "fixed_cost_increase"
    percent: float
    category_ids: list[CategoryId]
    from_date: LocalDate
    to_date: LocalDate

ScenarioAdjustment = Annotated[
    Union[DelayIncomeScenario, RevenueDropScenario, ExtraExpenseScenario, FixedCostIncreaseScenario],
    Field(discriminator="kind")
]

class ScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    adjustments: list[ScenarioAdjustment]

class ScenarioDelta(BaseModel):
    end_balance: Money
    maximum_shortage: Money
    first_gap_shift_days: int | None = None

class ScenarioResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: EntityId
    base_forecast_id: EntityId
    name: str
    adjustments: list[ScenarioAdjustment]
    forecast: ForecastSnapshot
    delta: ScenarioDelta

class PlanActualDay(BaseModel):
    model_config = ConfigDict(extra="ignore")
    date: LocalDate
    planned_income: Money
    actual_income: Money | None = None
    planned_expense: Money
    actual_expense: Money | None = None
    planned_balance: Money
    actual_balance: Money | None = None
    net_flow_variance: Money | None = None
    overdue_event_ids: list[EntityId] = []

class PlanActualQuery(DateRangeQuery):
    forecast_id: EntityId
    granularity: Literal["day", "week"] | None = "day"

class PlanActualResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    forecast_id: EntityId
    data_revision: int
    days: list[PlanActualDay]
    overdue_events: list[PlannedEvent]
    updated_forecast_id: EntityId | None = None

class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    training_end_date: LocalDate
    test_days: Literal[30, 60, 90] = 30
    folds: int = Field(default=3, ge=1, le=5)
    models: list[ModelId] = ["seasonal_naive", "weekday_mean", "ridge_calendar"]

class ModelMetrics(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: ModelId
    mae_income: Money
    mae_expense: Money
    mae_balance: Money
    wape_cashflow: float | None = None
    interval_empirical_coverage: Probability | None = None
    gap_precision: Probability | None = None
    gap_recall: Probability | None = None
    average_warning_lead_days: float | None = None
    gap_brier_score: float | None = None
    evaluated_days: int

class EvaluationFold(BaseModel):
    train_end: LocalDate
    test_start: LocalDate
    test_end: LocalDate

class EvaluationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: EntityId
    company_id: EntityId
    created_at: UtcTimestamp
    data_revision: int
    split_method: Literal["rolling_origin"] = "rolling_origin"
    test_days: int
    folds: list[EvaluationFold]
    metrics: list[ModelMetrics]
    selected_model: ModelId
    selection_reason: str
    limitations: list[str]

class AdviceRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    business_type: str = "Розничная торговля / Кофейня"

class RecommendationItem(BaseModel):
    title: str
    rationale: str
    related_event_ids: list[EntityId] = []
    priority: Literal["high", "normal"] = "normal"

class AdviceResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    forecast_id: EntityId
    mode: Literal["llm", "rules"]
    analysis: str
    recommendations: list[RecommendationItem]
    generated_at: UtcTimestamp

IntegrationKind = Literal["bank", "accounting", "cash_register", "marketplace", "registry", "invoice", "contract", "tax"]

class Integration(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: EntityId
    kind: IntegrationKind
    name: str
    status: Literal["connected", "disconnected", "error", "not_configured"]
    last_sync_at: UtcTimestamp | None = None
    last_error: str | None = None
    supported_entities: list[Literal[
        "transactions", "invoices", "contracts", "taxes", "loans",
        "payroll", "payment_orders", "counterparty_status"
    ]]

class IntegrationListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[Integration]

class IntegrationSyncResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    integration_id: EntityId
    imported_count: int
    data_revision: int
    synced_at: UtcTimestamp
