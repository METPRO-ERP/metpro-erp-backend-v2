-- Client default payment terms (release R4), 2026-11-27. Additive only. Plan: claude/four-items-scoping-plan-2026-11-27.md section 5.
-- Nothing reads or writes the new columns until PAYMENT_DUE_ENABLED is on (R4 rides that switch; no new switch).
--   clients.default_credit_days    : NULL = no default; 0 = due on the invoice date; 1..365 = days of credit (mirrors quotes.credit_days).
--   clients.default_payment_terms  : free text, mirrors quotes.payment_terms.
-- A default is only a prefill at quote creation. Existing quotes and invoices are never touched (snapshot discipline).
BEGIN;

ALTER TABLE public.clients ADD COLUMN IF NOT EXISTS default_credit_days smallint NULL
    CONSTRAINT clients_default_credit_days_check CHECK (default_credit_days BETWEEN 0 AND 365);
ALTER TABLE public.clients ADD COLUMN IF NOT EXISTS default_payment_terms text NULL;

COMMIT;
