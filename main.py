import os
import base64
import requests
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

if not all([SUPABASE_URL, SUPABASE_KEY, ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
    raise RuntimeError("❌ Variables d'environnement manquantes.")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")


# -----------------------
# UTILS
# -----------------------
def normalize_date(date_value):
    if not date_value:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        dt = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def find_or_create_partner(email, name):
    partner = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "search_read",
        [[["email", "=", email]]],
        {"limit": 1}
    )
    if partner:
        return partner[0]["id"]

    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "create",
        [{"name": name or email, "email": email, "customer_rank": 1}]
    )


def find_product(package_id):
    product = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "name", "list_price"], "limit": 1}
    )
    return product[0] if product else None


def find_odoo_order(order_ref):
    res = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "search",
        [[["client_order_ref", "=", order_ref]]],
        {"limit": 1}
    )
    return res[0] if res else None


def confirm_order(order_id):
    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "action_confirm",
            [[order_id]]
        )
        print(f"✅ Commande confirmée : {order_id}")
    except Exception as e:
        print(f"❌ Erreur confirmation {order_id}: {e}")


# -----------------------
# SYNC PRODUITS
# -----------------------
def sync_products():
    print("🚀 Sync produits Airalo...")

    packages = supabase.table("airalo_packages").select("*").execute().data
    for row in packages:
        package_id = row["airalo_id"]
        name = row["name"]
        region = row["region"] or ""
        price = row["final_price_eur"]

        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "search",
            [[["default_code", "=", package_id]]],
            {"limit": 1}
        )

        vals = {
            "name": f"{name} [{region}]",
            "default_code": package_id,
            "list_price": float(price),
            "type": "service",
            "sale_ok": True,
            "purchase_ok": False,
        }

        if existing:
            models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "product.product", "write",
                [[existing[0]], vals]
            )
            print(f"🔁 Produit mis à jour : {package_id}")
        else:
            models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "product.product", "create",
                [vals]
            )
            print(f"✅ Produit créé : {package_id}")

    print("🎉 Produits synchronisés.")


# -----------------------
# SYNC COMMANDES
# -----------------------
def sync_airalo_orders():
    print("🛒 Sync commandes Airalo...")
    rows = supabase.table("airalo_orders").select("*").execute().data

    for row in rows:
        email = row["email"]
        package_id = row["package_id"]
        order_ref = row["order_id"]

        product = find_product(package_id)
        if not product:
            print(f"❌ Produit introuvable Airalo : {package_id}")
            continue

        if find_odoo_order(order_ref):
            continue

        partner_id = find_or_create_partner(email, email)

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "create",
            [{
                "partner_id": partner_id,
                "client_order_ref": order_ref,
                "date_order": normalize_date(row["created_at"]),
                "order_line": [(0, 0, {
                    "product_id": product["id"],
                    "name": product["name"],
                    "product_uom_qty": 1,
                    "price_unit": product["list_price"]
                })]
            }]
        )
        print(f"🟢 Commande Airalo créée : {order_ref}")

    print("✅ Commandes Airalo synchronisées.")


# -----------------------
# SYNC PAIEMENTS STRIPE
# -----------------------
def sync_stripe_payments():
    print("💳 Sync paiements Stripe...")

    rows = supabase.table("orders").select("*").execute().data

    for row in rows:
        order_ref = row["order_id"]
        status = row["status"]
        package_id = row["package_id"]
        email = row["email"]

        if status != "completed":
            continue

        if not order_ref:
            print("⚠️ Paiement sans order_id, ignoré.")
            continue

        odoo_order = find_odoo_order(order_ref)
        if not odoo_order:
            # pas trouvé → on crée via fusion email + package_id
            product = find_product(package_id)
            if not product:
                print(f"❌ Produit introuvable (Stripe) : {package_id}")
                continue

            partner_id = find_or_create_partner(email, email)

            odoo_order = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "sale.order", "create",
                [{
                    "partner_id": partner_id,
                    "client_order_ref": order_ref,
                    "date_order": normalize_date(row["created_at"]),
                    "order_line": [(0, 0, {
                        "product_id": product["id"],
                        "name": product["name"],
                        "product_uom_qty": 1,
                        "price_unit": product["list_price"],
                    })],
                }]
            )
            print(f"🟢 Commande créée via Stripe : {order_ref}")

        # CONFIRMATION
        confirm_order(odoo_order)

    print("💰 Paiements Stripe synchronisés.")


# -----------------------
# MAIN
# -----------------------
if __name__ == "__main__":
    print("🚀 Début synchronisation Supabase → Odoo")

    sync_products()
    sync_airalo_orders()
    sync_stripe_payments()

    print("✅ Synchronisation terminée")
