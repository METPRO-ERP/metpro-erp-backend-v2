-- Rollback for 2026-11-28_inventory_p1.sql. Turn INVENTORY_ENABLED off first.
-- Refuses once any movement exists: from the first posted movement, the switch is the rollback.
-- With no movement it drops the five new tables, their guards and the sequence, and nothing else.
-- Items, lots and the seeded warehouse that exist at that point are dropped with them (a NOTICE reports the counts).
BEGIN;
DO $$
DECLARE
    n_items int;
    n_lots int;
BEGIN
    IF to_regclass('public.inv_movements') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM public.inv_movements LIMIT 1) THEN
            RAISE EXCEPTION 'an inventory movement exists; the INVENTORY_ENABLED switch is the rollback now';
        END IF;
    END IF;
    n_items := 0;
    n_lots := 0;
    IF to_regclass('public.inv_items') IS NOT NULL THEN
        SELECT count(*) INTO n_items FROM public.inv_items;
    END IF;
    IF to_regclass('public.inv_lots') IS NOT NULL THEN
        SELECT count(*) INTO n_lots FROM public.inv_lots;
    END IF;
    RAISE NOTICE 'dropping inventory tables with % item(s) and % lot(s), no movements', n_items, n_lots;
END $$;
DROP TABLE IF EXISTS public.inv_movement_lines;
DROP TABLE IF EXISTS public.inv_movements;
DROP TABLE IF EXISTS public.inv_lots;
DROP TABLE IF EXISTS public.inv_items;
DROP TABLE IF EXISTS public.inv_warehouses;
DROP FUNCTION IF EXISTS public.inv_movement_lines_guard();
DROP FUNCTION IF EXISTS public.inv_lots_guard();
DROP FUNCTION IF EXISTS public.inv_movements_guard();
DROP FUNCTION IF EXISTS public.inv_items_guard();
DROP SEQUENCE IF EXISTS public.inv_movement_number_seq;
COMMIT;
