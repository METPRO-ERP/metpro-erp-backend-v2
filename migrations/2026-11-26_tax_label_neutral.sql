-- Neutral label for the 1.8% construction tax type: drop the law citation from the label.
-- Approved by Fabian 2026-11-26. Tax logic, rates and calculations do NOT change.
--
-- Rename only. The code value construction_17_2017 never changes (backend tax/service.py, estimator/service.py,
-- frontend lib/taxRule.ts, and every quote and invoice carry it). Rate, active, is_default, sort_order are untouched.
-- Documents keep their stored label snapshot: quotes.tax_label and invoices.tax_label are NOT touched here.
-- Production on 2026-11-26: 14 quotes and 1 invoice hold the old snapshot; new and re-saved documents take this label.
--
-- Guarded: it renames only a row that still holds the original seed label. A label edited by an admin is left alone.
-- Idempotent: a second run matches no row. Additive in effect: one value in one row of one table, no schema change.
BEGIN;

UPDATE public.tax_types
   SET label = 'Construcción (1.8%)', updated_at = now()
 WHERE code = 'construction_17_2017'
   AND label = 'Ley 17-2017 (1.8%)';

COMMIT;
