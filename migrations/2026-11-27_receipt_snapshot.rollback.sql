-- Rollback for 2026-11-27_receipt_snapshot.sql. Turn RECEIPT_ENABLED off first.
-- Refuses once any payment carries a snapshot figure: from the first payment recorded after the R3 backend deploy
-- (decision D-Y: the snapshot is always written), the RECEIPT_ENABLED switch is the rollback, not this script.
-- to_jsonb keeps the guard safe if a column is already gone (a second run).
-- Redeploying the previous backend (710fefe) is safe with the columns present: they are nullable and unused there.
BEGIN;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.invoice_payments
               WHERE to_jsonb(invoice_payments)->>'paid_so_far_after' IS NOT NULL
                  OR to_jsonb(invoice_payments)->>'pending_after' IS NOT NULL LIMIT 1) THEN
        RAISE EXCEPTION 'a payment carries a receipt snapshot; the RECEIPT_ENABLED switch is the rollback now';
    END IF;
END $$;
ALTER TABLE public.invoice_payments DROP CONSTRAINT IF EXISTS invoice_payments_snapshot_chk;
ALTER TABLE public.invoice_payments DROP COLUMN IF EXISTS pending_after;
ALTER TABLE public.invoice_payments DROP COLUMN IF EXISTS paid_so_far_after;
COMMIT;
