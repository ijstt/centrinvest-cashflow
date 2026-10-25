# Financial correctness and production release remediation

Baseline: `be5fef499e26d058613f5e1b7669a2513a248de7`.

New obligations are added completely unless they replace an identified historical component. Remaining baseline budgets are separate from new events, so independent commitments cannot consume each other. Category scenarios use stored expense components. Weekly, monthly and semi-monthly patterns are projected from past-only evidence; a GET request no longer changes the forecast model.

Import, manual events and external events share completion of reconciliation and forecast refresh. Matching is company-scoped and deterministic; exact document references support late/partial/overpayments. Weak matching requires an unambiguous counterparty, exact outstanding amount and no conflicting reference. Ambiguity remains unmatched. A historical forecast reconstructs payments known at its cutoff instead of reading a future paid status.

Snapshots persist request provenance, statistical baseline, calibration inputs and category components in an additive nullable `calculation_state` JSON column. Startup upgrades existing snapshot tables without rewriting their financial data. Future-only updates validate the historical fingerprint and reuse the fitted baseline and calibration; risk is recomputed to reflect the changed future flows. Changes to historical facts trigger full retraining and refresh automatic opening balances. Manual balances and planned-event settings retain their original meaning.

Runtime and evaluation use the same forecast implementation. Backtests do not save snapshots or use future events. The control CLI shares statistical models and honest held-out calibration, and its Brier/coverage calculation uses bootstrap/conformal estimates instead of a different Gaussian risk surrogate. The interval's 80% value is nominal; empirical coverage must be measured on independent data, without asserting universal finite-sample validity for dependent time series.

Company row locks cover import deduplication and snapshot reads. Domain input rejects impossible dates, nonfinite amounts, nonpositive obligations, probabilities outside [0,1] and negative revisions. Empty-history legacy requests no longer fabricate finances. Provider errors log exception types rather than potentially sensitive exception bodies.

## Verification

- Complete suite on isolated PostgreSQL 16: **170 passed**. A disposable test database and container were removed afterward.
- Includes nine formerly failing acceptance cases and regression checks for reuse without fitting, partial/overpayments, reverse ingestion order, conflicting references, future payment leakage, semi-monthly payments, independent recurring components and concurrent import idempotency.
- Frontend: **31 passed**, TypeScript/Vite build passed, generated contract check passed (30 operations, 83 schemas).
- Control CLI completed with `fixtures/statements/C001_logistics.csv` and produced JSON.
- CI now provisions an isolated PostgreSQL service. Docker installs the package after copying its source tree.
- Two older tests were corrected to assert required behavior: a negative revision must be rejected; a category-specific baseline scenario must provide category provenance.

## Deployment

The existing VPS uses containers `hackrnd_backend`, `hackrnd-frontend` and `hackrnd_postgres`. Deployment backs up the database, backend source and frontend before updating. New hashed frontend assets are copied before atomically replacing the HTML entry point; older assets remain available to open browser sessions. Backend rollback restores the saved source. The additive JSON column remains compatible with the previous application version.

External DirectBank/OFD/FNS connectors remain explicitly sandbox implementations. This release does not claim live connector credentials or guaranteed forecasting accuracy on unseen businesses. Reconciliation deliberately avoids automatically resolving ambiguous payments.
