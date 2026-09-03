# Razorpay Online Payment Setup

## 1. Install
Run:
    pip install -r requirements.txt

## 2. Configure .env
Copy `.env.example` to `.env` and fill in your MySQL settings and Razorpay TEST MODE
Key ID / Key Secret. Do not commit `.env`.

## 3. Database
The Flask application automatically adds these columns to `bills`:
- gateway_order_id
- gateway_payment_id
- payment_reference
- gateway_signature

You can also run `payment_migration.sql` manually if preferred.

## 4. Test flow
1. Start the Flask application.
2. Log in.
3. Open Billing Management.
4. Find a Pending bill.
5. Click Pay Online.
6. Razorpay Checkout opens.
7. Complete the payment using Razorpay's TEST MODE credentials/options.
8. The browser response is verified on the server.
9. The bill becomes Online / Paid only after signature verification.

## 5. Webhook
Configure your Razorpay webhook endpoint as:

    https://YOUR-DOMAIN/razorpay/webhook

Use the same value as `RAZORPAY_WEBHOOK_SECRET` in `.env`.
For local development, use a secure tunnel such as your chosen HTTPS tunneling tool;
do not expose your development server directly to the public internet.

## Security
- API secrets are server-side environment variables.
- The browser never receives the API secret.
- Checkout responses are signature-verified on the server.
- Webhooks are HMAC-verified.
- Payment amount is checked against the bill total for webhook events.
- A failed/cancelled checkout leaves the bill Pending.
