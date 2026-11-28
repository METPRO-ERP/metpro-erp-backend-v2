-- Inventory stock ledger, Phase 1 (2026-11-28). Plan: claude/inventory-module-plan-2026-11-28.md. Decisions I-D2 to I-D16.
-- NEW TABLES ONLY: no existing table, column, constraint or trigger is altered. Nothing reads or writes these
-- tables until INVENTORY_ENABLED is on (absent = off: every inventory route answers 404 FEATURE_DISABLED).
-- Requires products, quotes and projects (all already in production).
--
-- Ledger rules enforced here, not only in code:
--   * movement lines and lots are immutable (no UPDATE, no DELETE);
--   * a movement header changes only once, by the void stamp (voided_at / voided_by / void_reason);
--   * the sign of a line is bound to the kind of its header (composite foreign key + CHECK);
--   * units are exactly Ft, Lb, Unidad; an issue needs a quote (job reference, I-D9);
--   * one opening batch per warehouse (I-D11: locked after posting); one reversal per movement.
-- Rules enforced in the service (they need business time or running totals): no future dates (I-D13), no negative
-- stock (I-D6), weighted-average valuation (I-D15), roles (I-D8).
BEGIN;

CREATE SEQUENCE IF NOT EXISTS public.inv_movement_number_seq START 1;

-- ---------------------------------------------------------------------------------------------------------------
-- Warehouses. One seeded row; the dimension exists from day one so a second location is a data row (Q8).
CREATE TABLE IF NOT EXISTS public.inv_warehouses (
    id          bigserial   PRIMARY KEY,
    code        text        NOT NULL UNIQUE CHECK (btrim(code) <> ''),
    name        text        NOT NULL CHECK (btrim(name) <> ''),
    is_active   boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);
INSERT INTO public.inv_warehouses (code, name) VALUES ('PRINCIPAL', 'Almacén principal') ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------------------------------------------------------
-- Items: stocked things only (raw materials and consumables). 'remnant' arrives in Phase 2 by widening the CHECK.
CREATE TABLE IF NOT EXISTS public.inv_items (
    id                  bigserial     PRIMARY KEY,
    sku                 text          NOT NULL UNIQUE CHECK (btrim(sku) <> ''),
    name                text          NOT NULL CHECK (btrim(name) <> ''),
    kind                text          NOT NULL CHECK (kind IN ('raw_material', 'consumable')),
    stock_unit          text          NOT NULL CHECK (stock_unit IN ('Ft', 'Lb', 'Unidad')),
    tracks_lots         boolean       NOT NULL DEFAULT false,            -- true for coils: lot reference and weight at entry
    standard_length_ft  numeric(10,2) CHECK (standard_length_ft IS NULL OR standard_length_ft > 0),
    product_id          bigint        REFERENCES public.products(id) ON DELETE SET NULL,   -- optional link, informational
    is_active           boolean       NOT NULL DEFAULT true,
    is_dummy            boolean       NOT NULL DEFAULT false,            -- live-test items: out of valuation totals (I-D14)
    notes               text,
    created_by          text,
    created_at          timestamptz   NOT NULL DEFAULT now(),
    updated_at          timestamptz   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS inv_items_product_idx ON public.inv_items (product_id) WHERE product_id IS NOT NULL;

-- Coil lots (I-D5): the lot reference and the weight attribute captured at entry. Weight is an attribute only; the
-- stock quantity of a coil is its feet. entry_quantity and entry_cost are stored for information.
CREATE TABLE IF NOT EXISTS public.inv_lots (
    id              bigserial     PRIMARY KEY,
    item_id         bigint        NOT NULL REFERENCES public.inv_items(id) ON DELETE RESTRICT,
    lot_ref         text          NOT NULL CHECK (btrim(lot_ref) <> ''),
    weight_lb       numeric(14,2) NOT NULL CHECK (weight_lb > 0),
    entry_quantity  numeric(14,2) NOT NULL CHECK (entry_quantity > 0),
    entry_cost      numeric(16,2) NOT NULL CHECK (entry_cost > 0),
    created_by      text,
    created_at      timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT inv_lots_item_ref_key UNIQUE (item_id, lot_ref)
);

-- ---------------------------------------------------------------------------------------------------------------
-- Movement headers. Append-only; the only allowed change is the one-time void stamp.
CREATE TABLE IF NOT EXISTS public.inv_movements (
    id                    bigserial   PRIMARY KEY,
    number                bigint      NOT NULL DEFAULT nextval('public.inv_movement_number_seq') UNIQUE,   -- label M-000001
    kind                  text        NOT NULL CHECK (kind IN
                              ('opening', 'receipt', 'issue_job', 'adjustment', 'void', 'transfer_out', 'transfer_in')),
    warehouse_id          bigint      NOT NULL REFERENCES public.inv_warehouses(id) ON DELETE RESTRICT,
    occurred_on           date        NOT NULL,
    quote_id              text        REFERENCES public.quotes(quote_id) ON DELETE RESTRICT,     -- job reference (I-D9)
    project_id            integer     REFERENCES public.projects(id) ON DELETE RESTRICT,         -- optional
    source_type           text        NOT NULL DEFAULT 'manual' CHECK (source_type IN ('manual', 'po', 'conduce', 'invoice', 'bill')),
    source_id             text,                                                                   -- soft reference, no foreign key
    supplier_id           bigint,                                                                 -- soft reference: suppliers can be cleaned
    supplier_ref          text,                                                                   -- supplier invoice number
    reason                text,
    note                  text,
    reverses_movement_id  bigint      REFERENCES public.inv_movements(id) ON DELETE RESTRICT,
    created_by            text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    voided_at             timestamptz,
    voided_by             text,
    void_reason           text,
    CONSTRAINT inv_movements_id_kind_key UNIQUE (id, kind),
    CONSTRAINT inv_movements_issue_job_check CHECK (kind <> 'issue_job' OR quote_id IS NOT NULL),
    CONSTRAINT inv_movements_reason_check CHECK (kind NOT IN ('adjustment', 'void') OR length(btrim(coalesce(reason, ''))) > 0),
    CONSTRAINT inv_movements_void_link_check CHECK ((kind = 'void') = (reverses_movement_id IS NOT NULL)),
    CONSTRAINT inv_movements_void_stamp_check CHECK (
        (voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL)
        OR (voided_at IS NOT NULL AND length(btrim(coalesce(void_reason, ''))) > 0))
);
CREATE UNIQUE INDEX IF NOT EXISTS inv_movements_one_opening_key ON public.inv_movements (warehouse_id) WHERE kind = 'opening';
CREATE UNIQUE INDEX IF NOT EXISTS inv_movements_one_reversal_key ON public.inv_movements (reverses_movement_id) WHERE reverses_movement_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS inv_movements_quote_idx ON public.inv_movements (quote_id) WHERE quote_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS inv_movements_wh_date_idx ON public.inv_movements (warehouse_id, occurred_on);

-- Movement lines. quantity is signed in the item's own stock unit; value is signed money (DOP, ex-ITBIS) stored at
-- posting. On-hand quantity = SUM(quantity), on-hand value = SUM(value), per item and warehouse. Nothing is recomputed.
CREATE TABLE IF NOT EXISTS public.inv_movement_lines (
    id           bigserial     PRIMARY KEY,
    movement_id  bigint        NOT NULL,
    kind         text          NOT NULL,                       -- copy of the header kind, bound by the composite key below
    position     integer       NOT NULL CHECK (position >= 1),
    item_id      bigint        NOT NULL REFERENCES public.inv_items(id) ON DELETE RESTRICT,
    lot_id       bigint        REFERENCES public.inv_lots(id) ON DELETE RESTRICT,
    quantity     numeric(14,2) NOT NULL CHECK (quantity <> 0),
    unit_cost    numeric(14,4) NOT NULL CHECK (unit_cost >= 0),
    value        numeric(16,2) NOT NULL,
    CONSTRAINT inv_lines_movement_kind_fkey FOREIGN KEY (movement_id, kind)
        REFERENCES public.inv_movements (id, kind) ON DELETE RESTRICT,
    CONSTRAINT inv_lines_position_key UNIQUE (movement_id, position),
    CONSTRAINT inv_lines_kind_sign_check CHECK (
        (kind IN ('opening', 'receipt', 'transfer_in') AND quantity > 0 AND value > 0)
        OR (kind IN ('issue_job', 'transfer_out') AND quantity < 0 AND value <= 0)
        OR (kind = 'adjustment' AND ((quantity > 0 AND value > 0) OR (quantity < 0 AND value <= 0)))
        OR (kind = 'void' AND ((quantity > 0 AND value >= 0) OR (quantity < 0 AND value <= 0)))),
    CONSTRAINT inv_lines_lot_kind_check CHECK (lot_id IS NULL OR kind IN ('opening', 'receipt', 'void'))
);
CREATE INDEX IF NOT EXISTS inv_lines_item_idx ON public.inv_movement_lines (item_id);
CREATE INDEX IF NOT EXISTS inv_lines_lot_idx ON public.inv_movement_lines (lot_id) WHERE lot_id IS NOT NULL;

ALTER TABLE public.inv_warehouses     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inv_items          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inv_lots           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inv_movements      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inv_movement_lines ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------------------------------------------
-- Guards. A mistake is corrected by a void (reversal lines) or an adjustment, never by an edit.
CREATE OR REPLACE FUNCTION public.inv_movement_lines_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'inv_movement_lines are immutable (movement %); void the movement or post an adjustment', OLD.movement_id;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS inv_movement_lines_guard_trg ON public.inv_movement_lines;
CREATE TRIGGER inv_movement_lines_guard_trg BEFORE UPDATE OR DELETE ON public.inv_movement_lines
    FOR EACH ROW EXECUTE FUNCTION public.inv_movement_lines_guard();

CREATE OR REPLACE FUNCTION public.inv_lots_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'inv_lots are immutable (lot %)', OLD.lot_ref;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS inv_lots_guard_trg ON public.inv_lots;
CREATE TRIGGER inv_lots_guard_trg BEFORE UPDATE OR DELETE ON public.inv_lots
    FOR EACH ROW EXECUTE FUNCTION public.inv_lots_guard();

CREATE OR REPLACE FUNCTION public.inv_movements_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'inventory movement M-% is a ledger record; it cannot be deleted, void it instead', OLD.number;
    END IF;
    IF OLD.voided_at IS NOT NULL THEN
        RAISE EXCEPTION 'inventory movement M-% is already void', OLD.number;
    END IF;
    IF NEW.number IS DISTINCT FROM OLD.number OR NEW.kind IS DISTINCT FROM OLD.kind
       OR NEW.warehouse_id IS DISTINCT FROM OLD.warehouse_id OR NEW.occurred_on IS DISTINCT FROM OLD.occurred_on
       OR NEW.quote_id IS DISTINCT FROM OLD.quote_id OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.source_type IS DISTINCT FROM OLD.source_type OR NEW.source_id IS DISTINCT FROM OLD.source_id
       OR NEW.supplier_id IS DISTINCT FROM OLD.supplier_id OR NEW.supplier_ref IS DISTINCT FROM OLD.supplier_ref
       OR NEW.reason IS DISTINCT FROM OLD.reason OR NEW.note IS DISTINCT FROM OLD.note
       OR NEW.reverses_movement_id IS DISTINCT FROM OLD.reverses_movement_id
       OR NEW.created_by IS DISTINCT FROM OLD.created_by OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'inventory movement M-% is a ledger record; only voiding is allowed', OLD.number;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS inv_movements_guard_trg ON public.inv_movements;
CREATE TRIGGER inv_movements_guard_trg BEFORE UPDATE OR DELETE ON public.inv_movements
    FOR EACH ROW EXECUTE FUNCTION public.inv_movements_guard();

-- Once an item has movements its identity (sku, kind, unit, lot tracking, dummy flag) is frozen: the stored
-- quantities and values only make sense in that unit. Name, notes, active flag, length and product link stay editable.
CREATE OR REPLACE FUNCTION public.inv_items_guard() RETURNS trigger AS $$
BEGIN
    IF (NEW.sku IS DISTINCT FROM OLD.sku OR NEW.kind IS DISTINCT FROM OLD.kind OR NEW.stock_unit IS DISTINCT FROM OLD.stock_unit
        OR NEW.tracks_lots IS DISTINCT FROM OLD.tracks_lots OR NEW.is_dummy IS DISTINCT FROM OLD.is_dummy)
       AND EXISTS (SELECT 1 FROM public.inv_movement_lines WHERE item_id = OLD.id) THEN
        RAISE EXCEPTION 'inventory item % has movements; its sku, kind, unit, lot tracking and dummy flag cannot change', OLD.sku;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS inv_items_guard_trg ON public.inv_items;
CREATE TRIGGER inv_items_guard_trg BEFORE UPDATE ON public.inv_items
    FOR EACH ROW EXECUTE FUNCTION public.inv_items_guard();

COMMIT;
