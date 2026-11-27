-- Rollback for 2026-11-27_clients_default_terms.sql. Turn PAYMENT_DUE_ENABLED off first.
-- Refuses once any client carries a default term: from the first stored default, the PAYMENT_DUE_ENABLED switch is the rollback.
-- to_jsonb keeps the guard safe if a column is already gone (a second run).
BEGIN;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.clients
               WHERE to_jsonb(clients)->>'default_credit_days' IS NOT NULL
                  OR to_jsonb(clients)->>'default_payment_terms' IS NOT NULL LIMIT 1) THEN
        RAISE EXCEPTION 'a client carries a default payment term; the PAYMENT_DUE_ENABLED switch is the rollback now';
    END IF;
END $$;
ALTER TABLE public.clients DROP CONSTRAINT IF EXISTS clients_default_credit_days_check;
ALTER TABLE public.clients DROP COLUMN IF EXISTS default_credit_days;
ALTER TABLE public.clients DROP COLUMN IF EXISTS default_payment_terms;
COMMIT;
