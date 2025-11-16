import os
import base64
import requests
import xmlrpc.client
import socket
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime
import time

# -----------------------
# CONFIG
# -----------------------
load_dotenv()

# Timeout global pour Odoo (30s)
socket.setdefaulttimeout(30)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

if not all([SUPABASE_URL, SUPABASE_KEY, ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
    raise RuntimeError("❌ Variables d'environnement manquantes.")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Clients XML-RPC
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise RuntimeError("❌ Authentification Odoo échouée")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# -----------------------
# HELPERS
# -----------------------

def rpc(model, method, args=None, kwargs=None, retries=3):
    """Appels XML-RPC Odoo avec retries intelligents."""
    args = args or []
    kwargs = kwargs or {}
    for attempt in range(1, retries + 1):
        try:
            return models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                model,
                method,
                args,
                kwargs
            )
        except Exception as e:
            print(f"⚠️ Tentative {attempt}/{retries} échouée ({model}.{method}) : {e}")
            if attempt == retries:
                print(f"❌ Abandon après {retries} tentatives pour {model}.{method}")
                return None
            time.sleep(2)  # pause avant retry

def normalize_date(date_value):
    """Normalise une date ISO en format Odoo."""
    if not date_value:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        dt = datetime.fromisoformat(str(date_value).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def paginate_supabase(table):
    """Récupère tout le contenu d'une table, même > 1000 lignes."""
    results = []
    chunk = 1000
    offset = 0

    while True:
        data = (
            supabase.table(table)
            .select("*")
            .range(offset, offset + chunk - 1)
            .execute()
            .data
        )
        if not data:
            break
        results.extend(data)
        offset += chunk

    return results

def find_or_create_partner(email, name=None):
    if not email:
        return None

    partners = rpc(
        "res.partner",
        "search_read",
        args=[[["email", "=", email]]],
        kwargs={"fields": ["id"], "limit": 1}
    )
    if partners:
        return partners[0]["id"]

    partner_id = rpc(
        "res.partner",
        "create",
        args=[[{"name": name or email, "email": email, "customer_rank": 1}]]
    )

    print(f"👤 Partenaire créé : {email} (ID {partner_id})")
    return partner_id

def find_product(package_id):
    if not package_id:
        return None
    product = rpc(
        "product.product",
        "search_read",
        args=[[["default_code", "=", package_id]]],
        kwargs={"fields": ["id", "name", "list_price"], "limit": 1},
    )
    return product[0] if product else None

def find_odoo_order(order_ref):
    res = rpc(
        "sale.order",
        "search",
        args=[[["client_order_ref", "=", order_ref]]],
        kwargs={"limit": 1}
    )
    return res[0] if res else None

def confirm_order(order_id):
    if not order_id:
        return
    rpc("sale.order", "action_confirm", args=[[order_id]])
    print(f"✅ Commande confirmée : {order_id}")

# -----------------------
# SYNC PRODUITS
# -----------------------

def sync_products():
    print("🚀 Sync produits Airalo...")
    packages = paginate_supabase("airalo_packages")
    print(f"📦 {len(packages)} packages trouvés")

    for row in packages:
        package_id = row.get("airalo_id")
        name = row.get("name")
        region = row.get("region") or ""
        price = row.get("final_price_eur") or row.get("price_eur") or 0.0

        if not package_id or not name:
            continue

        existing_ids = rpc(
            "product.product",
            "search",
            args=[[["default_code", "=", package_id]]],
            kwargs={"limit": 1},
        )

        vals = {
            "name": f"{name} [{region}]" if region else name,
            "default_code": package_id,
            "list_price": float(price),
            "type": "service",
            "sale_ok": True,
            "purchase_ok": False,
        }

        if existing_ids:
            rpc("product.product", "write", args=[[existing_ids[0]], vals])
            print(f"🔁 Produit mis à jour : {package_id}")
        else:
            rpc("product.product", "create", args=[[vals]])
            print(f"✅ Produit créé : {package_id}")

    print("🎉 Produits synchronisés.")

# -----------------------
# SYNC COMMANDES AIRALO
# -----------------------

def sync_airalo_orders():
    print("🛒 Sync commandes Airalo...")
    rows = paginate_supabase("airalo_orders")
    print(f"📄 {len(rows)} lignes airalo_orders")

    for row in rows:
        email = row.get("email")
        package_id = row.get("package_id")
        order_ref = row.get("order_id")
        created_at = normalize_date(row.get("created_at"))

        if not email or not package_id or not order_ref:
            continue

        if find_odoo_order(order_ref):
            continue

        product = find_product(package_id)
        if not product:
            print(f"❌ Produit introuvable Airalo : {package_id}")
            continue

        partner_id = find_or_create_partner(email, email)

        rpc(
            "sale.order",
            "create",
            args=[[
                {
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
                }
            ]]
        )
        print(f"🟢 Commande Airalo créée : {order_ref}")

    print("✅ Commandes Airalo synchronisées.")

# -----------------------
# SYNC PAIEMENTS STRIPE
# -----------------------

def sync_stripe_payments():
    print("💳 Sync paiements Stripe...")
    rows = paginate_supabase("orders")
    print(f"📄 {len(rows)} lignes orders")

    for row in rows:
        order_ref = row.get("order_id")
        if not order_ref:
            continue

        status = row.get("status", "")
        email = row.get("email")
        package_id = row.get("package_id")
        created_at = normalize_date(row.get("created_at"))

        if status != "completed":
            continue

        odoo_order_id = find_odoo_order(order_ref)

        if not odoo_order_id:
            if not email or not package_id:
                print(f"⚠️ Impossible de créer la commande pour {order_ref}")
                continue

            product = find_product(package_id)
            if not product:
                print(f"❌ Produit introuvable : {package_id}")
                continue

            partner_id = find_or_create_partner(email, email)

            odoo_order_id = rpc(
                "sale.order",
                "create",
                args=[[
                    {
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
                    }
                ]]
            )
            print(f"🟢 Commande créée : {order_ref}")

        confirm_order(odoo_order_id)

    print("💰 Paiements Stripe synchronisés.")

# -----------------------
# MAIN
# -----------------------

if __name__ == "__main__":
    print("🚀 Début synchronisation")

    try:
        sync_products()
    except Exception as e:
        print(f"❌ Erreur sync_products : {e}")

    try:
        sync_airalo_orders()
    except Exception as e:
        print(f"❌ Erreur sync_airalo_orders : {e}")

    try:
        sync_stripe_payments()
    except Exception as e:
        print(f"❌ Erreur sync_stripe_payments : {e}")

    print("✅ Synchronisation terminée")
