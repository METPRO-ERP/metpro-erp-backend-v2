-- Rollback for 2026-11-26_payment_due.sql. Turn PAYMENT_DUE_ENABLED off first.
-- Refuses once any quote carries credit_days, any invoice carries due_date, or any audit row exists:
-- from the first stored value, the PAYMENT_DUE_ENABLED switch is the rollback.
-- to_jsonb keeps the guard safe if a column is already gone (a second run).
BEGIN;
DO $$
DECLARE has_audit boolean;
BEGIN
    IF EXISTS (SELECT 1 FROM public.quotes WHERE to_jsonb(quotes)->>'credit_days' IS NOT NULL LIMIT 1) THEN
        RAISE EXCEPTION 'a quote carries credit_days; the PAYMENT_DUE_ENABLED switch is the rollback now';
    END IF;
    IF EXISTS (SELECT 1 FROM public.invoices WHERE to_jsonb(invoices)->>'due_date' IS NOT NULL LIMIT 1) THEN
        RAISE EXCEPTION 'an invoice carries due_date; the PAYMENT_DUE_ENABLED switch is the rollback now';
    END IF;
    IF to_regclass('public.invoice_due_date_audit') IS NOT NULL THEN
        -- dynamic SQL: the table may already be gone on a second run, and a static reference would fail at planning
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.invoice_due_date_audit LIMIT 1)' INTO has_audit;
        IF has_audit THEN
            RAISE EXCEPTION 'an invoice_due_date_audit row exists; the PAYMENT_DUE_ENABLED switch is the rollback now';
        END IF;
    END IF;
END $$;
DROP TRIGGER IF EXISTS invoice_due_date_audit_guard_trg ON public.invoice_due_date_audit;
DROP FUNCTION IF EXISTS public.invoice_due_date_audit_guard();
DROP TABLE IF EXISTS public.invoice_due_date_audit;
ALTER TABLE public.invoices DROP COLUMN IF EXISTS due_date;
ALTER TABLE public.quotes   DROP CONSTRAINT IF EXISTS quotes_credit_days_check;
ALTER TABLE public.quotes   DROP COLUMN IF EXISTS credit_days;
COMMIT;
