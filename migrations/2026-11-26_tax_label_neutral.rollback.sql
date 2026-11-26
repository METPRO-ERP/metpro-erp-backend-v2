-- Rollback for 2026-11-26_tax_label_neutral.sql: restores the original seed label of construction_17_2017.
-- Guarded: it restores only a row that still holds the migration's label. A label edited since is left alone.
-- No data refusal is needed: only this one label value moves, and no document snapshot is touched either way.
BEGIN;

UPDATE public.tax_types
   SET label = 'Ley 17-2017 (1.8%)', updated_at = now()
 WHERE code = 'construction_17_2017'
   AND label = 'Construcción (1.8%)';

COMMIT;
