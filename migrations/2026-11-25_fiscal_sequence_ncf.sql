-- Fiscal sequence (NCF), 2026-11-25. Additive only. Plan v2.1 section 2.
-- One new table for the batch (series B, type 01), one audit table, three nullable columns on invoices.
-- No row is inserted. No production value is seeded: the admin enters the batch at activation.
-- Nothing reads or writes any of this until FISCAL_NCF_ENABLED is on (activation endpoints are admin-only).
BEGIN;

CREATE TABLE IF NOT EXISTS public.fiscal_sequences (
    id                    serial      PRIMARY KEY,
    series                text        NOT NULL,
    comprobante_type      text        NOT NULL CHECK (comprobante_type = '01'),   -- type 01 only in this release
    next_number           bigint,
    range_start           bigint,
    range_end             bigint,
    expiry_date           date,
    active                boolean     NOT NULL DEFAULT false,
    locked                boolean     NOT NULL DEFAULT false,                     -- true from the first NCF assigned
    activated_at          timestamptz,
    activated_by_user_id  uuid        REFERENCES public.users(id) ON DELETE SET NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fiscal_sequences_range_ck CHECK (
        next_number IS NULL OR range_start IS NULL OR range_end IS NULL
        OR (range_start <= range_end AND next_number >= range_start AND next_number <= range_end + 1)
    )
);

-- One active batch per (series, type).
CREATE UNIQUE INDEX IF NOT EXISTS fiscal_sequences_one_active_idx
    ON public.fiscal_sequences (series, comprobante_type) WHERE active;

-- Permanent lock (DECIDED): once locked, the batch identity can never change and the row can never be deleted.
-- Only next_number may move, and only forward.
CREATE OR REPLACE FUNCTION public.fiscal_sequences_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.locked THEN
            RAISE EXCEPTION 'fiscal_sequences row % is locked (an NCF was issued); it cannot be deleted', OLD.id;
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.locked THEN
        IF NEW.locked IS DISTINCT FROM true
           OR NEW.series IS DISTINCT FROM OLD.series
           OR NEW.comprobante_type IS DISTINCT FROM OLD.comprobante_type
           OR NEW.range_start IS DISTINCT FROM OLD.range_start
           OR NEW.range_end IS DISTINCT FROM OLD.range_end
           OR NEW.expiry_date IS DISTINCT FROM OLD.expiry_date
           OR NEW.active IS DISTINCT FROM OLD.active
           OR NEW.next_number IS NULL OR NEW.next_number < OLD.next_number THEN
            RAISE EXCEPTION 'fiscal_sequences row % is locked (an NCF was issued); only next_number may advance', OLD.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS fiscal_sequences_guard_trg ON public.fiscal_sequences;
CREATE TRIGGER fiscal_sequences_guard_trg BEFORE UPDATE OR DELETE ON public.fiscal_sequences
    FOR EACH ROW EXECUTE FUNCTION public.fiscal_sequences_guard();

-- Audit: who activated or corrected the batch, when, with which values. Append-only.
CREATE TABLE IF NOT EXISTS public.fiscal_sequence_audit (
    id                 bigserial   PRIMARY KEY,
    fiscal_sequence_id integer     NOT NULL REFERENCES public.fiscal_sequences(id),
    action             text        NOT NULL CHECK (action IN ('activate', 'correct')),
    values_json        jsonb       NOT NULL,
    actor_user_id      uuid        REFERENCES public.users(id) ON DELETE SET NULL,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION public.fiscal_sequence_audit_block_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS fiscal_sequence_audit_append_only ON public.fiscal_sequence_audit;
CREATE TRIGGER fiscal_sequence_audit_append_only BEFORE UPDATE OR DELETE ON public.fiscal_sequence_audit
    FOR EACH ROW EXECUTE FUNCTION public.fiscal_sequence_audit_block_change();

ALTER TABLE public.fiscal_sequences      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.fiscal_sequence_audit ENABLE ROW LEVEL SECURITY;

-- Invoice columns: all nullable, no default. Existing rows are untouched (no backfill).
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS ncf                text;
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS fiscal_sequence_id integer REFERENCES public.fiscal_sequences(id);
ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS ncf_assigned_at    timestamptz;

CREATE UNIQUE INDEX IF NOT EXISTS invoices_ncf_key ON public.invoices (ncf) WHERE ncf IS NOT NULL;

COMMIT;
