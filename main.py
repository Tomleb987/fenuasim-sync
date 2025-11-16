import os
import base64
import requests
import xmlrpc.client
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# 🔐 CONFIG
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

if not all([SUPABASE_URL, SUPABASE_KEY, ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
    raise RuntimeError("❌ Variables d'environnement manquantes")

# 🔗 Connexion Supabase
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# 🔗 Connexion Odoo
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise Exception("❌ Connexion Odoo échouée")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# 🗺️ MAPPING anciens codes produits Airalo
PACKAGE_MAP = {
    "discover+-365days-20gb": "discover-365days-20gb",
    "discover+-60days-5gb": "discover-60days-5gb",
    "discover+-7days-1gb": "discover-7days-1gb",
}

# 🧠 Reconstruire le vrai package pour un TOPUP
def extract_real_package_id(row):
    package_id = str(row.get("package_id")).strip()
    transaction_type = row.get("transaction_type")
    airalo_order_id = row.get("airalo_order_id")

    # Cas 1 : mapping ancien code → nouveau
    if package_id in PACKAGE_MAP:
        return PACKAGE_MAP[package_id]

    # Cas 2 : TOPUP → reconstruire le vrai produit
    if transaction_type == "topup" and airalo_order_id:
        base = airalo_order_id.split("-topup")[0]
        return base

    return package_id


# 🕒 Normalisation des dates
def normalize_odoo_datetime(value):
    if not value:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 📸 Convertir image
def get_image_base64_from_url(url):
    if not url:
        return None
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return base64.b64encode(r.content).decode("utf-8")
    except:
        pass
    return None


# 🧹 Nettoyage doublons
def remove_duplicate_products():
    print("🧹 Suppression des doublons produits...")
    products = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "!=", False]]],
        {"fields": ["id", "default_code"], "limit": 5000}
    )

    from collections import defaultdict
    grouped = defaultdict(list)
    for p in products:
        grouped[p["default_code"]].append(p["id"])

    deleted = 0
    for code, ids in grouped.items():
        if len(ids) > 1:
            try:
                models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "product.product", "unlink",
                    [ids[1:]]
                )
                deleted += len(ids) - 1
            except:
                pass

    print(f"✅ Nettoyage terminé ({deleted} doublons supprimés)")


# 🔄 Upsert produit
def upsert_product(row):
    package_id = row.get("airalo_id")
    name = row.get("name")
    region = row.get("region") or ""
    price = row.get("final_price_eur") or row.get("price_eur") or 0

    if not package_id or not name:
        return

    full_name = f"{name} [{region}]" if region else name

    existing = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "product_tmpl_id", "image_1920"], "limit": 1}
    )

    image_url = row.get("image_url")
    image_b64 = None
    if image_url:
        image_b64 = get_image_base64_from_url(image_url)

    # UPDATE
    if existing:
        tmpl_id = existing[0]["product_tmpl_id"][0]
        vals = {
            "name": full_name,
            "list_price": float(price),
        }
        if image_b64 and not existing[0]["image_1920"]:
            vals["image_1920"] = image_b64

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.template", "write",
            [[tmpl_id], vals]
        )
        print(f"🔁 Produit mis à jour : {package_id}")
        return

    # CREATE
    vals = {
        "name": full_name,
        "default_code": package_id,
        "list_price": float(price),
        "type": "service",
        "sale_ok": True,
        "purchase_ok": False,
    }
    if image_b64:
        vals["image_1920"] = image_b64

    models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "create",
        [vals]
    )

    print(f"✅ Produit créé : {package_id}")


# 📦 Sync produits Airalo
def sync_airalo_packages():
    print("🚀 Sync produits Airalo...")
    rows = supabase.table("airalo_packages").select("*").execute().data
    for r in rows:
        upsert_product(r)
    print("🎉 Produits Airalo synchronisés.")


# 👤 Partner
def get_or_create_partner(email, full_name):
    partner = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "search",
        [[["email", "=", email]]],
        {"limit": 1}
    )
    if partner:
        return partner[0]

    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "create",
        [{"name": full_name or email, "email": email, "customer_rank": 1}]
    )


# 🔎 Trouver un produit
def find_product(package_id):
    p = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "name", "list_price"], "limit": 1}
    )
    return p[0] if p else None


# 🛒 Sync commandes Airalo
def sync_airalo_orders():
    print("🛒 Sync commandes Airalo...")
    rows = supabase.table("airalo_orders").select("*").execute().data

    for row in rows:
        order_ref = row.get("order_id") or row.get("id")
        if not order_ref:
            continue

        package_id = extract_real_package_id(row)

        email = row.get("email")
        full_name = f"{row.get('prenom','')} {row.get('nom','')}".strip()
        date = normalize_odoo_datetime(row.get("created_at"))

        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "search",
            [[["client_order_ref", "=", order_ref]]],
            {"limit": 1}
        )
        if existing:
            continue

        partner_id = get_or_create_partner(email, full_name)
        product = find_product(package_id)

        if not product:
            print(f"❌ Produit introuvable Airalo : {package_id}")
            continue

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "create",
            [{
                "partner_id": partner_id,
                "client_order_ref": order_ref,
                "date_order": date,
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


# 🛒 Sync commandes standard
def sync_orders():
    print("🛒 Sync commandes standard...")
    rows = supabase.table("orders").select("*").execute().data

    for row in rows:
        order_ref = row.get("order_id") or row.get("id")
        if not order_ref:
            continue

        package_id = extract_real_package_id(row)

        email = row.get("email")
        full_name = f"{row.get('prenom','')} {row.get('nom','')}".strip()
        date = normalize_odoo_datetime(row.get("created_at"))

        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "search",
            [[["client_order_ref", "=", order_ref]]],
            {"limit": 1}
        )
        if existing:
            continue

        partner_id = get_or_create_partner(email, full_name)
        product = find_product(package_id)

        if not product:
            print(f"❌ Produit introuvable commande standard : {package_id}")
            continue

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "create",
            [{
                "partner_id": partner_id,
                "client_order_ref": order_ref,
                "date_order": date,
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

        print(f"🟢 Commande standard créée : {order_ref}")


# 🚀 MAIN
if __name__ == "__main__":
    print("🚀 Début synchronisation Supabase → Odoo")

    remove_duplicate_products()
    sync_airalo_packages()
    sync_airalo_orders()
    sync_orders()

    print("✅ Synchronisation terminée")
