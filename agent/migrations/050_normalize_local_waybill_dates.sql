-- Local entry historically stored slash dates, while date filters use ISO.
-- Preserve existing waybill numbers and all other business fields.
UPDATE waybills
SET open_date = REPLACE(open_date, '/', '-')
WHERE source IN ('manual', 'ocr')
  AND open_date REGEXP '^[0-9]{4}/[0-9]{2}/[0-9]{2}$';
