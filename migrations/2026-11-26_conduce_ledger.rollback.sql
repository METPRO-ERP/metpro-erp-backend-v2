-- Rollback for 2026-11-26_conduce_ledger.sql. Turn CONDUCE_LEDGER_ENABLED off first.
-- Refuses once any ledger conduce exists: from the first delivery created under the switch, the switch is the rollback.
-- Legacy conduces rows are never touched.
BEGIN;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.conduces WHERE delivery_seq IS NOT NULL LIMIT 1) THEN
        RAISE EXCEPTION 'a ledger conduce exists; the CONDUCE_LEDGER_ENABLED switch is the rollback now';
    END IF;
END $$;
DROP TRIGGER IF EXISTS conduces_ledger_guard_trg ON public.conduces;
DROP FUNCTION IF EXISTS public.conduces_ledger_guard();
DROP TABLE IF EXISTS public.conduce_lines;
DROP FUNCTION IF EXISTS public.conduce_lines_guard();
DROP INDEX IF EXISTS public.conduces_quote_idx;
DROP INDEX IF EXISTS public.conduces_quote_seq_key;
ALTER TABLE public.conduces DROP CONSTRAINT IF EXISTS conduces_void_shape_check;
ALTER TABLE public.conduces DROP CONSTRAINT IF EXISTS conduces_ledger_shape_check;
ALTER TABLE public.conduces DROP CONSTRAINT IF EXISTS conduces_quote_id_fkey;
ALTER TABLE public.conduces DROP COLUMN IF EXISTS void_reason;
ALTER TABLE public.conduces DROP COLUMN IF EXISTS voided_by;
ALTER TABLE public.conduces DROP COLUMN IF EXISTS voided_at;
ALTER TABLE public.conduces DROP COLUMN IF EXISTS created_by;
ALTER TABLE public.conduces DROP COLUMN IF EXISTS notes;
ALTER TABLE public.conduces DROP COLUMN IF EXISTS delivery_date;
ALTER TABLE public.conduces DROP COLUMN IF EXISTS delivery_seq;
ALTER TABLE public.conduces DROP COLUMN IF EXISTS quote_id;
COMMIT;
