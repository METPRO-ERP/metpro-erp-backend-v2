-- Payment terms days and invoice due date, 2026-11-26. Additive only. Plan: claude/workstream-bc-plan-2026-11-26.md section 4.3
-- (decisions D1 to D7 and D11). Nothing reads or writes the new columns and table until PAYMENT_DUE_ENABLED is on.
--   quotes.credit_days   : NULL = no structured term; 0 = due on the invoice date; 1..365 = days of credit.
--   invoices.due_date    : a STORED snapshot written once at conversion (invoice issue date + the quote's credit_days).
--   invoice_due_date_audit: append-only record of every admin edit of due_date (old -> new, mandatory note, actor).
-- No fiscal, tax, numbering or money column is touched.
BEGIN;

ALTER TABLE public.quotes   ADD COLUMN IF NOT EXISTS credit_days smallint NULL
    CONSTRAINT quotes_credit_days_check CHECK (credit_days BETWEEN 0 AND 365);
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS due_date date NULL;

CREATE TABLE IF NOT EXISTS public.invoice_due_date_audit (
    id            bigserial   PRIMARY KEY,
    invoice_id    integer     NOT NULL REFERENCES public.invoices(id),
    old_due_date  date        NULL,
    new_due_date  date        NOT NULL,
    note          text        NOT NULL CONSTRAINT invoice_due_date_audit_note_check CHECK (length(btrim(note)) > 0),
    changed_by    uuid        NULL,
    changed_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT invoice_due_date_audit_changed_check CHECK (old_due_date IS DISTINCT FROM new_due_date)
);
CREATE INDEX IF NOT EXISTS invoice_due_date_audit_invoice_idx ON public.invoice_due_date_audit (invoice_id, id);
ALTER TABLE public.invoice_due_date_audit ENABLE ROW LEVEL SECURITY;

-- Append-only: a mistake is corrected by a new edit with its own note, never by changing or deleting a row.
CREATE OR REPLACE FUNCTION public.invoice_due_date_audit_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'invoice_due_date_audit is append-only (invoice %); record a new edit instead', OLD.invoice_id;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS invoice_due_date_audit_guard_trg ON public.invoice_due_date_audit;
CREATE TRIGGER invoice_due_date_audit_guard_trg BEFORE UPDATE OR DELETE ON public.invoice_due_date_audit
    FOR EACH ROW EXECUTE FUNCTION public.invoice_due_date_audit_guard();

COMMIT;
