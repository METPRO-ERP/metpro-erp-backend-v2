-- Phased invoicing, 2026-11-25. Additive only. Plan v2.1 section 3.
-- Requires 2026-11-25_fiscal_sequence_ncf.sql (phases are only usable with FISCAL_NCF_ENABLED on).
-- Nothing reads or writes this until PHASED_INVOICING_ENABLED is on.
BEGIN;

ALTER TABLE public.quotes   ADD COLUMN IF NOT EXISTS billing_mode text NOT NULL DEFAULT 'FULL';
ALTER TABLE public.quotes   DROP CONSTRAINT IF EXISTS quotes_billing_mode_check;
ALTER TABLE public.quotes   ADD CONSTRAINT quotes_billing_mode_check CHECK (billing_mode IN ('FULL', 'PHASED'));

CREATE TABLE IF NOT EXISTS public.invoice_phases (
    id                  serial        PRIMARY KEY,
    quote_id            text          NOT NULL REFERENCES public.quotes(quote_id) ON DELETE RESTRICT,
    phase_order         integer       NOT NULL CHECK (phase_order >= 1),
    label               text          NOT NULL,
    amount_input_type   text          NOT NULL CHECK (amount_input_type IN ('percent', 'fixed', 'final')),
    amount_input_value  numeric,
    amount_snapshot     numeric(14,2) NOT NULL CHECK (amount_snapshot > 0),   -- tax-included total of the phase
    itbis_snapshot      numeric(14,2) NOT NULL CHECK (itbis_snapshot >= 0),   -- stored pro-rata ITBIS (F1)
    note                text,
    invoice_id          bigint        REFERENCES public.invoices(id) ON DELETE RESTRICT,
    status              text          NOT NULL DEFAULT 'Planned' CHECK (status IN ('Planned', 'Invoiced')),
    created_at          timestamptz   NOT NULL DEFAULT now(),
    created_by_user_id  uuid          REFERENCES public.users(id) ON DELETE SET NULL,
    CONSTRAINT invoice_phases_quote_order_key UNIQUE (quote_id, phase_order),
    CONSTRAINT invoice_phases_itbis_le_total CHECK (itbis_snapshot <= amount_snapshot)
);
CREATE INDEX IF NOT EXISTS invoice_phases_quote_idx ON public.invoice_phases (quote_id);
CREATE UNIQUE INDEX IF NOT EXISTS invoice_phases_invoice_key ON public.invoice_phases (invoice_id) WHERE invoice_id IS NOT NULL;
-- One open (Planned) phase per quote at a time.
CREATE UNIQUE INDEX IF NOT EXISTS invoice_phases_one_planned_idx ON public.invoice_phases (quote_id) WHERE status = 'Planned';

-- Once a phase has an invoice it is immutable and cannot be deleted. Planned -> Invoiced (setting invoice_id) is allowed.
CREATE OR REPLACE FUNCTION public.invoice_phases_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.invoice_id IS NOT NULL THEN
            RAISE EXCEPTION 'invoice_phases row % has an invoice; it cannot be deleted', OLD.id;
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.invoice_id IS NOT NULL THEN
        RAISE EXCEPTION 'invoice_phases row % has an invoice; it cannot be edited', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS invoice_phases_guard_trg ON public.invoice_phases;
CREATE TRIGGER invoice_phases_guard_trg BEFORE UPDATE OR DELETE ON public.invoice_phases
    FOR EACH ROW EXECUTE FUNCTION public.invoice_phases_guard();

ALTER TABLE public.invoice_phases ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.invoices ADD COLUMN IF NOT EXISTS phase_id integer REFERENCES public.invoice_phases(id) ON DELETE RESTRICT;

COMMIT;
