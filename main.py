import os
import base64
import requests
import xmlrpc.client
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# 🔐 Config Supabase & Odoo
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

if not all([SUPABASE_URL, SUPABASE_KEY, ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
    raise RuntimeError("❌ Variables d'environnement manquantes. Vérifie SUPABASE_* et ODOO_*")

# 🔗 Connexions
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise Exception("❌ Connexion Odoo échouée")
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")


# 🕒 Normalisation des dates pour Odoo SaaS
def normalize_odoo_datetime(value):
    if not value:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            return value
        except Exception:
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 🌍 Charger company_id + pricelist_id
def get_company_and_pricelist(partner_id):
    user_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.users", "read", [uid],
        {"fields": ["company_id"]},
    )[0]
    company_id = user_data["company_id"][0]

    partner_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "read", [partner_id],
        {"fields": ["property_product_pricelist"]},
    )[0]
    pricelist_id = partner_data["property_product_pricelist"][0]

    return company_id, pricelist_id


# 📸 Image URL → Base64
def get_image_base64_from_url(url: str):
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            return base64.b64encode(resp.content).decode("utf-8")
    except Exception:
        pass
    return None


# 🧹 Suppression doublons produits
def remove_duplicate_products():
    print("🧹 Suppression des doublons produits...")

    products = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "!=", False]]],
        {"fields": ["id", "default_code"], "limit": 5000},
    )

    from collections import defaultdict
    grouped = defaultdict(list)
    for p in products:
        grouped[p["default_code"]].append(p["id"])

    total_deleted = 0
    for code, ids in grouped.items():
        if len(ids) > 1:
            try:
                models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "product.product", "unlink",
                    [ids[1:]],
                )
                total_deleted += len(ids) - 1
                print(f"🗑️ Doublons supprimés : {code}")
            except Exception:
                pass

    print(f"✅ Nettoyage terminé ({total_deleted} doublons supprimés)")


# 🔍 Trouver produit existant
def find_product(package_id):
    product = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "name", "list_price"], "limit": 1},
    )
    return product[0] if product else None


# 🆕 Création autom. produit minimal
def create_minimal_product(package_id, price):
    print(f"⚠️ Produit absent, création automatique : {package_id}")

    vals = {
        "name": package_id,
        "default_code": package_id,
        "list_price": float(price or 0.0),
        "type": "service",
        "sale_ok": True,
        "purchase_ok": False,
    }

    product_id = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "create",
        [vals],
    )

    return {
        "id": product_id,
        "name": package_id,
        "list_price": float(price or 0.0),
    }


# 🔄 Upsert produits Airalo
def upsert_product(row: dict):
    package_id = row.get("airalo_id")
    name = row.get("name")
    region = row.get("region") or ""
    price = row.get("final_price_eur") or row.get("price_eur") or 0.0

    if not package_id or not name:
        return

    full_name = f"{name} [{region}]" if region else name

    existing = find_product(package_id)

    vals_template = {
        "name": full_name,
        "list_price": float(price),
        "description": row.get("description") or "",
    }

    if existing:
        tmpl_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "read",
            [existing["id"]],
            {"fields": ["product_tmpl_id"]},
        )[0]["product_tmpl_id"][0]

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.template", "write",
            [[tmpl_id], vals_template],
        )
        print(f"🔁 Produit mis à jour : {package_id}")
        return

    # Création produit
    vals = {
        **vals_template,
        "default_code": package_id,
        "type": "service",
        "sale_ok": True,
    }

    models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "create",
        [vals],
    )

    print(f"✅ Produit créé : {package_id}")


# 🛒 Sync commandes Airalo
def sync_airalo_orders():
    print("🛒 Sync commandes Airalo...")

    rows = supabase.table("airalo_orders").select("*").execute().data

    for row in rows:
        order_ref = row.get("order_id")
        if not order_ref:
            continue

        email = row.get("email")
        package_id = row.get("package_id")
        created_at = normalize_odoo_datetime(row.get("created_at"))
        price = row.get("price_eur") or row.get("final_price_eur") or 0

        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "search",
            [[["client_order_ref", "=", order_ref]]],
            {"limit": 1},
        )
        if existing:
            continue

        partner_id = find_or_create_partner(email, row.get("prenom", "") + " " + row.get("nom", ""))
        product = find_product(package_id) or create_minimal_product(package_id, price)

        company_id, pricelist_id = get_company_and_pricelist(partner_id)

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "create",
            [{
                "partner_id": partner_id,
                "client_order_ref": order_ref,
                "date_order": created_at,
                "company_id": company_id,
                "pricelist_id": pricelist_id,
                "order_line": [
                    (0, 0, {
                        "product_id": product["id"],
                        "name": product["name"],
                        "product_uom_qty": 1,
                        "price_unit": product["list_price"],
                    })
                ],
            }],
        )

        print(f"🟢 Commande Airalo créée : {order_ref}")


# 🛒 Sync commandes standard
def sync_orders():
    print("🛒 Sync commandes standard...")

    rows = supabase.table("orders").select("*").execute().data

    for row in rows:
        order_ref = row.get("order_id")
        if not order_ref:
            continue

        email = row.get("email")
        package_id = row.get("package_id")
        price = row.get("price") or row.get("amount") or 0
        created_at = normalize_odoo_datetime(row.get("created_at"))

        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "search",
            [[["client_order_ref", "=", order_ref]]],
            {"limit": 1},
        )
        if existing:
            continue

        partner_id = find_or_create_partner(email, row.get("prenom", "") + " " + row.get("nom", ""))
        product = find_product(package_id) or create_minimal_product(package_id, price)

        company_id, pricelist_id = get_company_and_pricelist(partner_id)

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "create",
            [{
                "partner_id": partner_id,
                "client_order_ref": order_ref,
                "date_order": created_at,
                "company_id": company_id,
                "pricelist_id": pricelist_id,
                "order_line": [
                    (0, 0, {
                        "product_id": product["id"],
                        "name": product["name"],
                        "product_uom_qty": 1,
                        "price_unit": product["list_price"],
                    })
                ],
            }],
        )

        print(f"🟢 Commande standard créée : {order_ref}")


# 🚀 MAIN
if __name__ == "__main__":
    print("🚀 Début synchronisation Supabase → Odoo")
    remove_duplicate_products()
    sync_airalo_packages()
    sync_airalo_orders()
    sync_orders()
    print("✅ Synchronisation terminée")
