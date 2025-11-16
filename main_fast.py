import os
import xmlrpc.client
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# -----------------------
# CONFIG
# -----------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")


# -----------------------
# UTILS
# -----------------------
def dt(x):
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def find_partner(email):
    res = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "search_read",
        [[["email", "=", email]]],
        {"fields": ["id"], "limit": 1}
    )
    return res[0]["id"] if res else None

def create_partner(email):
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "create",
        [{"name": email, "email": email, "customer_rank": 1}]
    )

def get_or_create_partner(email):
    return find_partner(email) or create_partner(email)

def find_order(order_ref):
    res = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "search",
        [[["client_order_ref", "=", order_ref]]],
        {"limit": 1},
    )
    return res[0] if res else None

def find_product(package_id):
    res = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "name", "list_price"], "limit": 1},
    )
    return res[0] if res else None

def confirm_order(order_id):
    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "action_confirm",
            [[order_id]],
        )
        print(f"✔️ Order confirmed: {order_id}")
    except Exception as e:
        print("❌ Error confirming:", e)

def create_invoice(order_id):
    try:
        inv_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "action_create_invoice",
            [[order_id]],
        )
        return inv_id
    except:
        pass

    invoices = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "account.move", "search",
        [[["invoice_origin", "=", order_id]]],
        {"limit": 1},
    )
    return invoices[0] if invoices else None

def validate_invoice(inv_id):
    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "account.move", "action_post",
            [[inv_id]],
        )
    except Exception as e:
        print("❌ Error validating invoice:", e)

def register_payment(inv_id, amount):
    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "account.payment.register", "create",
            [{
                "payment_date": datetime.now().strftime("%Y-%m-%d"),
                "amount": amount,
                "payment_method_line_id": 1,  # to adjust if needed
                "journal_id": 1,              # your bank journal
                "communication": "Stripe payment",
                "line_ids": [(6, 0, [inv_id])]
            }]
        )
    except Exception as e:
        print("❌ Error recording payment:", e)


# -----------------------
# SYNC STRIPE ORDERS ONLY (FAST)
# -----------------------
print("🚀 Début synchronisation rapide Supabase → Odoo")

rows = supabase.table("orders").select("*").execute().data

for r in rows:
    if r.get("status") != "completed":
        continue

    if not r.get("stripe_session_id"):
        # Ignore Airalo or incomplete orders
        continue

    order_ref = r.get("order_id")
    email = r.get("email")
    pkg = r.get("package_id")
    created = dt(r.get("created_at"))

    if not order_ref or not email or not pkg:
        continue

    product = find_product(pkg)
    if not product:
        print("❌ Product missing", pkg)
        continue

    partner = get_or_create_partner(email)
    odoo_order = find_order(order_ref)

    # If order exists, confirm it again
    if odoo_order:
        print("↪️ Existing Odoo order:", order_ref)
        confirm_order(odoo_order)

    # Otherwise create order
    else:
        print("🟢 Creating Odoo order:", order_ref)
        odoo_order = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "create",
            [{
                "partner_id": partner,
                "client_order_ref": order_ref,
                "date_order": created,
                "order_line": [
                    (0, 0, {
                        "product_id": product["id"],
                        "name": product["name"],
                        "product_uom_qty": 1,
                        "price_unit": product["list_price"],
                    })
                ],
            }]
        )
        confirm_order(odoo_order)

    # -----------------------
    # INVOICE + PAYMENT
    # -----------------------
    inv_id = create_invoice(odoo_order)
    if inv_id:
        validate_invoice(inv_id)
        register_payment(inv_id, float(product["list_price"]))
        print(f"💰 Payment recorded for {order_ref}")

print("✅ Synchronisation rapide terminée")
