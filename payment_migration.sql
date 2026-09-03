-- Online payment gateway fields for bills.
-- The Flask application also adds these automatically when it starts.

ALTER TABLE bills
    ADD COLUMN gateway_order_id VARCHAR(100) NULL,
    ADD COLUMN gateway_payment_id VARCHAR(100) NULL,
    ADD COLUMN payment_reference VARCHAR(150) NULL,
    ADD COLUMN gateway_signature VARCHAR(255) NULL;

CREATE INDEX idx_bills_gateway_order_id ON bills (gateway_order_id);
