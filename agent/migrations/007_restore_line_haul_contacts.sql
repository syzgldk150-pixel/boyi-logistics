-- Preserve the historical active-state repair without runtime schema/data mutation.
UPDATE line_haul_contacts
SET is_active = 1
WHERE is_active = 0;
