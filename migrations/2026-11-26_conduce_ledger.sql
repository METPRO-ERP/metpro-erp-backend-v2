-- Conduce delivery ledger, 2026-11-26. Additive only. Plan: claude/conduce-module-plan-2026-11-26.md section 2.8.
-- Nothing reads or writes the new columns and table until CONDUCE_LEDGER_ENABLED is on.
-- Legacy conduces rows (one per invoice, created by viewing a preview) are not touched.
BEGIN;

ALTER TABLE public.conduces ADD COLUMN IF NOT EXISTS quote_id      text;
ALTER TABLE public.conduces ADD COLUMN IF NOT EXISTS delivery_seq  integer;
ALTER TABLE public.conduces ADD COLUMN IF NOT EXISTS delivery_date date;
ALTER TABLE public.conduces ADD COLUMN IF NOT EXISTS notes         text;
ALTER TABLE public.conduces ADD COLUMN IF NOT EXISTS created_by    text;
ALTER TABLE public.conduces ADD COLUMN IF NOT EXISTS voided_at     timestamptz;
ALTER TABLE public.conduces ADD COLUMN IF NOT EXISTS voided_by     text;
ALTER TABLE public.conduces ADD COLUMN IF NOT EXISTS void_reason   text;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'conduces_quote_id_fkey') THEN
        ALTER TABLE public.conduces ADD CONSTRAINT conduces_quote_id_fkey
            FOREIGN KEY (quote_id) REFERENCES public.quotes(quote_id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'conduces_ledger_shape_check') THEN
        ALTER TABLE public.conduces ADD CONSTRAINT conduces_ledger_shape_check CHECK (
            (delivery_seq IS NULL AND quote_id IS NULL AND delivery_date IS NULL)
            OR (delivery_seq >= 1 AND quote_id IS NOT NULL AND delivery_date IS NOT NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'conduces_void_shape_check') THEN
        ALTER TABLE public.conduces ADD CONSTRAINT conduces_void_shape_check CHECK (
            (voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL)
            OR (voided_at IS NOT NULL AND delivery_seq IS NOT NULL AND length(btrim(coalesce(void_reason, ''))) > 0));
    END IF;
END $$;

-- One delivery sequence number per quote (E1, E2, ...). Legacy rows have no sequence.
CREATE UNIQUE INDEX IF NOT EXISTS conduces_quote_seq_key ON public.conduces (quote_id, delivery_seq) WHERE delivery_seq IS NOT NULL;
CREATE INDEX IF NOT EXISTS conduces_quote_idx ON public.conduces (quote_id) WHERE quote_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.conduce_lines (
    id               bigserial     PRIMARY KEY,
    conduce_id       uuid          NOT NULL REFERENCES public.conduces(id) ON DELETE CASCADE,
    quote_item_id    bigint        NOT NULL REFERENCES public.quote_items(id) ON DELETE RESTRICT,
    quantity         numeric(14,2) NOT NULL CHECK (quantity > 0),
    -- snapshot, so a reprint is identical whatever happens to the quote later
    product_name     text          NOT NULL,
    unit_of_measure  text,
    category         text,
    quoted_quantity  numeric(14,2) NOT NULL CHECK (quoted_quantity > 0),
    CONSTRAINT conduce_lines_conduce_item_key UNIQUE (conduce_id, quote_item_id),
    CONSTRAINT conduce_lines_within_quote CHECK (quantity <= quoted_quantity)
);
CREATE INDEX IF NOT EXISTS conduce_lines_item_idx ON public.conduce_lines (quote_item_id);
ALTER TABLE public.conduce_lines ENABLE ROW LEVEL SECURITY;

-- Ledger lines are immutable. A mistake is corrected by voiding the conduce, never by editing or deleting lines.
CREATE OR REPLACE FUNCTION public.conduce_lines_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'conduce_lines are immutable (conduce %); void the conduce instead', OLD.conduce_id;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS conduce_lines_guard_trg ON public.conduce_lines;
CREATE TRIGGER conduce_lines_guard_trg BEFORE UPDATE OR DELETE ON public.conduce_lines
    FOR EACH ROW EXECUTE FUNCTION public.conduce_lines_guard();

-- A ledger conduce (delivery_seq set) cannot be deleted; its only allowed change is the one-time void.
-- Legacy rows (no delivery_seq) behave exactly as before, including deletion with their invoice (B-05).
CREATE OR REPLACE FUNCTION public.conduces_ledger_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.delivery_seq IS NULL THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'conduce % is a delivery record; it cannot be deleted, void it instead', OLD.conduce_number;
    END IF;
    IF OLD.voided_at IS NOT NULL THEN
        RAISE EXCEPTION 'conduce % is already void', OLD.conduce_number;
    END IF;
    IF NEW.conduce_number IS DISTINCT FROM OLD.conduce_number OR NEW.quote_id IS DISTINCT FROM OLD.quote_id
       OR NEW.invoice_id IS DISTINCT FROM OLD.invoice_id OR NEW.delivery_seq IS DISTINCT FROM OLD.delivery_seq
       OR NEW.delivery_date IS DISTINCT FROM OLD.delivery_date OR NEW.notes IS DISTINCT FROM OLD.notes
       OR NEW.created_by IS DISTINCT FROM OLD.created_by OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'conduce % is a delivery record; only voiding is allowed', OLD.conduce_number;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS conduces_ledger_guard_trg ON public.conduces;
CREATE TRIGGER conduces_ledger_guard_trg BEFORE UPDATE OR DELETE ON public.conduces
    FOR EACH ROW EXECUTE FUNCTION public.conduces_ledger_guard();

COMMIT;
