-- R3 receipt snapshot, 2026-11-27. Additive only. Plan: claude/four-items-scoping-plan-2026-11-27.md section R3.
-- Two STORED figures per payment, written in the same INSERT as the payment, under the invoice row lock:
--   paid_so_far_after : sum of the live (non-voided) payments of the invoice INCLUDING this one, at recording time.
--   pending_after     : invoice total minus paid_so_far_after at that moment, never below zero.
-- Pre-R3 payments keep both NULL (decision D-X: no backfill, no receipt for them).
-- Immutability: the existing trigger invoice_payments_guard already refuses any UPDATE of a payment column other
-- than the one-time void fields, so once a row exists its two figures can never change. No new trigger.
-- Order of deploy: apply this migration BEFORE the backend that writes the columns (the INSERT names them).
-- No fiscal, tax, numbering or invoice column is touched.
BEGIN;

ALTER TABLE public.invoice_payments ADD COLUMN IF NOT EXISTS paid_so_far_after numeric(14,2) NULL;
ALTER TABLE public.invoice_payments ADD COLUMN IF NOT EXISTS pending_after     numeric(14,2) NULL;

ALTER TABLE public.invoice_payments DROP CONSTRAINT IF EXISTS invoice_payments_snapshot_chk;
ALTER TABLE public.invoice_payments ADD CONSTRAINT invoice_payments_snapshot_chk CHECK (
    (paid_so_far_after IS NULL AND pending_after IS NULL)
    OR (paid_so_far_after IS NOT NULL AND pending_after IS NOT NULL
        AND paid_so_far_after >= 0 AND pending_after >= 0)
);

COMMIT;
