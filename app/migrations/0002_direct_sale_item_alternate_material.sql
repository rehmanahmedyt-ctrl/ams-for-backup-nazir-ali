-- 0002_direct_sale_item_alternate_material.sql
--
-- Indexes and non-destructive metadata backfill for explicit supplied-material
-- vs original-booked-material tracking on direct_sale_item:
--   * material_id         -> actual physically supplied material (matches product_name)
--   * booked_material_id  -> original booked material (reference only)
--   * booked_material     -> original booked material name (reference only)
--   * is_booking          -> 1 when the slice fulfills an existing booking, else 0
--   * is_alternate        -> 1 when supplied material differs from booked_material
--
-- Safety guarantee:
--   * Does NOT modify any historical financial figures (amount, paid_amount,
--     discount, price_at_time), historical quantities (qty), historical stock
--     balances (material.total), or ambiguous pre-fix excess item names.
--     Reviewing and repairing pre-fix historical excess lines remains strictly
--     explicit via tools/repair_controlled/repair_alternate_booking_excess.py
--     with --sale-id <id> --confirm (which backs up the DB first).

CREATE INDEX IF NOT EXISTS ix_direct_sale_item_material_id
    ON direct_sale_item(material_id);

CREATE INDEX IF NOT EXISTS ix_direct_sale_item_booked_material_id
    ON direct_sale_item(booked_material_id);

-- Backfill material_id for actual supplied material where matched unambiguously.
UPDATE direct_sale_item
SET material_id = (
    SELECT m.id
    FROM material m
    WHERE LOWER(TRIM(m.name)) = LOWER(TRIM(direct_sale_item.product_name))
    LIMIT 1
)
WHERE material_id IS NULL
  AND product_name IS NOT NULL
  AND TRIM(product_name) <> '';

-- Backfill booking-fulfillment metadata from active booking_allocation links.
UPDATE direct_sale_item
SET is_booking = 1,
    booked_material = COALESCE(
        booked_material,
        (
            SELECT TRIM(bi.material_name)
            FROM booking_allocation ba
            JOIN booking_item bi ON bi.id = ba.booking_item_id
            WHERE ba.sale_item_id = direct_sale_item.id
              AND COALESCE(ba.is_void, 0) = 0
            ORDER BY ba.id ASC
            LIMIT 1
        )
    )
WHERE COALESCE(price_at_time, 0) <= 0
  AND EXISTS (
      SELECT 1
      FROM booking_allocation ba
      WHERE ba.sale_item_id = direct_sale_item.id
        AND COALESCE(ba.is_void, 0) = 0
  );

-- Legacy fallback: rate-0 booking slices created before booking_allocation existed,
-- matched unambiguously against active Booking Delivery OUT entries on the same sale.
UPDATE direct_sale_item
SET is_booking = 1,
    booked_material = COALESCE(
        booked_material,
        (
            SELECT TRIM(COALESCE(NULLIF(TRIM(e.booked_material), ''), e.material))
            FROM entry e
            WHERE e.source_module = 'sales'
              AND e.source_table = 'direct_sale'
              AND e.source_id = direct_sale_item.sale_id
              AND COALESCE(e.is_void, 0) = 0
              AND e.type = 'OUT'
              AND e.client_category = 'Booking Delivery'
              AND LOWER(TRIM(COALESCE(e.material, ''))) = LOWER(TRIM(COALESCE(direct_sale_item.product_name, '')))
              AND ABS(COALESCE(e.qty, 0) - COALESCE(direct_sale_item.qty, 0)) <= 0.0001
            ORDER BY e.id ASC
            LIMIT 1
        )
    )
WHERE COALESCE(is_booking, 0) = 0
  AND COALESCE(price_at_time, 0) <= 0
  AND EXISTS (
      SELECT 1
      FROM entry e
      WHERE e.source_module = 'sales'
        AND e.source_table = 'direct_sale'
        AND e.source_id = direct_sale_item.sale_id
        AND COALESCE(e.is_void, 0) = 0
        AND e.type = 'OUT'
        AND e.client_category = 'Booking Delivery'
        AND LOWER(TRIM(COALESCE(e.material, ''))) = LOWER(TRIM(COALESCE(direct_sale_item.product_name, '')))
        AND ABS(COALESCE(e.qty, 0) - COALESCE(direct_sale_item.qty, 0)) <= 0.0001
  );

-- Populate booked_material_id and is_alternate where booked_material is set.
UPDATE direct_sale_item
SET booked_material_id = COALESCE(
        booked_material_id,
        (
            SELECT m.id
            FROM material m
            WHERE LOWER(TRIM(m.name)) = LOWER(TRIM(direct_sale_item.booked_material))
            LIMIT 1
        )
    ),
    is_alternate = CASE
        WHEN LOWER(TRIM(COALESCE(booked_material, ''))) <> ''
         AND LOWER(TRIM(COALESCE(booked_material, ''))) <> LOWER(TRIM(COALESCE(product_name, '')))
        THEN 1
        ELSE COALESCE(is_alternate, 0)
    END
WHERE booked_material IS NOT NULL
  AND TRIM(booked_material) <> '';
