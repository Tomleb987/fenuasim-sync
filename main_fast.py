import os
import xmlrpc.client
from supabase import create_client
from datetime import datetime

# -------------------------------------
# CONFIG
# -------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Connexion Odoo
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")


# -------------------------------------
# UTILS
# -------------------------------------
def normalize_date(date_value):
    if not date_value:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        dt = datetime.fromisoformat(str(date_value).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def find_or_create_partner(email, name=None):
    res = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "search_read",
        [[["email", "=", email]]],
        {"fields": ["id"], "limit": 1}
    )
    if res:
        return res[0]["id"]

    partner_id = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "create",
        [{
            "name": name or email,
            "email": email,
            "customer_rank": 1
        }]
    )
    print(f"🆕 Partner créé : {email} (ID {partner_id})")
    return partner_id


def find_product(package_id):
    res = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "name", "list_price"], "limit": 1}
    )
    return res[0] if res else None


def find_odoo_order(order_ref):
    res = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "search",
        [[["client_order_ref", "=", order_ref]]],
        {"limit": 1}
    )
    return res[0] if res else None


def confirm_order(order_id):
    if not order_id:
        return
    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "action_confirm",
            [[order_id]]
        )
        print(f"✅ Commande confirmée : {order_id}")
    except Exception as e:
        print(f"⚠️ Erreur confirmation {order_id} : {e}")


# -------------------------------------
# SYNC AIRALO ORDERS
# -------------------------------------
def sync_airalo_orders():
    print("🔄 Sync Airalo orders…")
    rows = supabase.table("airalo_orders").select("*").execute().data

    for row in rows:
        order_ref = row.get("order_id")
        email = row.get("email")
        package_id = row.get("package_id")
        created_at = normalize_date(row.get("created_at"))

        if not order_ref or not email or not package_id:
            continue

        # déjà en Odoo ?
        if find_odoo_order(order_ref):
            continue

        product = find_product(package_id)
        if not product:
            print(f"❌ Produit introuvable Airalo : {package_id}")
            continue

        partner_id = find_or_create_partner(email, email)

        order_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "create",
            [{
                "partner_id": partner_id,
                "client_order_ref": order_ref,
                "date_order": created_at,
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
        print(f"🟢 Commande Airalo créée : {order_ref}")


# -------------------------------------
# SYNC STRIPE PAYMENTS
# -------------------------------------
def sync_stripe_payments():
    print("💳 Sync Stripe…")

    rows = supabase.table("orders").select("*").execute().data

    for row in rows:
        order_ref = row.get("order_id")
        status = row.get("status")
        email = row.get("email")
        package_id = row.get("package_id")
        created_at = normalize_date(row.get("created_at"))

        if not order_ref or status != "completed":
            continue

        odoo_order = find_odoo_order(order_ref)

        if not odoo_order:
            product = find_product(package_id)
            partner_id = find_or_create_partner(email, email)

            odoo_order = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "sale.order", "create",
                [{
                    "partner_id": partner_id,
                    "client_order_ref": order_ref,
                    "date_order": created_at,
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
            print(f"🟢 Commande Stripe créée : {order_ref}")

        confirm_order(odoo_order)


# -------------------------------------
# MAIN
# -------------------------------------
if __name__ == "__main__":
    print("🚀 FAST SYNC STARTED")
    sync_airalo_orders()
    sync_stripe_payments()
    print("✅ FAST SYNC DONE")
