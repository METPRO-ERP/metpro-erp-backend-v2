-- Rollback for 2026-11-25_phased_invoicing.sql. Turn PHASED_INVOICING_ENABLED off first.
-- Refuses if any phase exists or any quote is PHASED: once a phase exists, the switch is the rollback.
BEGIN;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.invoice_phases LIMIT 1) THEN
        RAISE EXCEPTION 'invoice_phases has rows; the phases switch is the rollback now';
    END IF;
    IF EXISTS (SELECT 1 FROM public.quotes WHERE billing_mode <> 'FULL' LIMIT 1) THEN
        RAISE EXCEPTION 'a quote is PHASED; the phases switch is the rollback now';
    END IF;
END $$;
ALTER TABLE public.invoices DROP COLUMN IF EXISTS phase_id;
DROP TABLE IF EXISTS public.invoice_phases;
DROP FUNCTION IF EXISTS public.invoice_phases_guard();
ALTER TABLE public.quotes DROP CONSTRAINT IF EXISTS quotes_billing_mode_check;
ALTER TABLE public.quotes DROP COLUMN IF EXISTS billing_mode;
COMMIT;
