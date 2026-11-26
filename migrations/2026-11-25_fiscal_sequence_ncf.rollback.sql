-- Rollback for 2026-11-25_fiscal_sequence_ncf.sql. Turn FISCAL_NCF_ENABLED off first.
-- Refuses if any invoice has an NCF or any fiscal sequence row is locked: once a number is issued,
-- the switch is the rollback and this SQL is NOT (register rule). Export the rows before any exception.
BEGIN;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.invoices WHERE ncf IS NOT NULL LIMIT 1) THEN
        RAISE EXCEPTION 'invoices.ncf has values; the NCF switch is the rollback now';
    END IF;
    IF EXISTS (SELECT 1 FROM public.fiscal_sequences WHERE locked LIMIT 1) THEN
        RAISE EXCEPTION 'a fiscal sequence is locked; the NCF switch is the rollback now';
    END IF;
END $$;
DROP INDEX IF EXISTS public.invoices_ncf_key;
ALTER TABLE public.invoices DROP COLUMN IF EXISTS ncf_assigned_at;
ALTER TABLE public.invoices DROP COLUMN IF EXISTS fiscal_sequence_id;
ALTER TABLE public.invoices DROP COLUMN IF EXISTS ncf;
DROP TABLE IF EXISTS public.fiscal_sequence_audit;
DROP TABLE IF EXISTS public.fiscal_sequences;
DROP FUNCTION IF EXISTS public.fiscal_sequence_audit_block_change();
DROP FUNCTION IF EXISTS public.fiscal_sequences_guard();
COMMIT;
