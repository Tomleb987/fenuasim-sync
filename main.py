import os
import base64
import datetime
import requests
import xmlrpc.client
from supabase import create_client
from dotenv import load_dotenv

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


# 🌍 Fonction helper — charger company_id et prix liste
def get_company_and_pricelist(partner_id):
    # Company du user (obligatoire pour sale.order en SaaS)
    user_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.users", "read",
        [uid],
        {"fields": ["company_id"]},
    )[0]
    company_id = user_data["company_id"][0]

    # Pricelist du partenaire (obligatoire en SaaS)
    partner_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "read",
        [partner_id],
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
        else:
            print(f"⚠️ Erreur téléchargement image ({resp.status_code}): {url}")
    except Exception as e:
        print(f"❌ Exception image {url}: {e}")
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
            to_delete = ids[1:]
            try:
                models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "product.product", "unlink",
                    [to_delete],
                )
                total_deleted += len(to_delete)
                print(f"🗑️ Doublons supprimés pour {code}: {len(to_delete)}")
            except Exception as e:
                print(f"❌ Erreur suppression doublons {code}: {e}")

    print(f"✅ Nettoyage terminé ({total_deleted} doublons supprimés)")


# 🔄 Upsert produit Airalo
def upsert_product(row: dict):
    package_id = row.get("airalo_id")
    name_base = row.get("name")
    region = row.get("region") or ""
    price = row.get("final_price_eur") or row.get("price_eur") or 0.0
    description = row.get("description") or ""
    data_amount = row.get("data_amount")
    data_unit = row.get("data_unit")
    validity_days = row.get("validity_days")
    image_url = row.get("image_url")

    if not package_id or not name_base:
        return

    name = f"{name_base} [{region}]" if region else name_base

    desc_lines = []
    if description:
        desc_lines.append(description)
    if data_amount and data_unit and validity_days:
        desc_lines.append(f"{data_amount} {data_unit} pour {validity_days} jours")
    if region:
        desc_lines.append(f"Région : {region}")

    full_description = "\n".join(desc_lines)

    # Vérifier si produit existant
    existing = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "product_tmpl_id", "image_1920"], "limit": 1},
    )

    image_base64 = None

    if existing:
        prod = existing[0]
        tmpl_id = prod["product_tmpl_id"][0]

        if not prod["image_1920"] and image_url:
            image_base64 = get_image_base64_from_url(image_url)

        vals = {
            "name": name,
            "list_price": float(price),
            "description": full_description,
        }
        if image_base64:
            vals["image_1920"] = image_base64

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.template", "write",
            [[tmpl_id], vals],
        )

        print(f"🔁 Produit mis à jour : {package_id} → {name} ({price} €)")
        return

    # Création nouvelle fiche produit
    if image_url:
        image_base64 = get_image_base64_from_url(image_url)

    vals = {
        "name": name,
        "default_code": package_id,
        "list_price": float(price),
        "type": "service",
        "sale_ok": True,
        "purchase_ok": False,
        "description": full_description,
    }
    if image_base64:
        vals["image_1920"] = image_base64

    product_id = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "create",
        [vals],
    )

    print(f"✅ Produit créé : {name} ({price} €) — ID {product_id}")


# 🟢 Sync produits Airalo
def sync_airalo_packages():
    print("🚀 Sync produits Airalo...")
    packages = supabase.table("airalo_packages").select("*").execute().data
    print(f"📦 {len(packages)} packages trouvés")

    for row in packages:
        upsert_product(row)

    print("🎉 Produits Airalo synchronisés.")


# 👤 Trouver ou créer un partenaire
def find_or_create_partner(email, full_name):
    partners = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "search",
        [[["email", "=", email]]],
        {"limit": 1},
    )
    if partners:
        return partners[0]

    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "create",
        [{"name": full_name or email, "email": email, "customer_rank": 1}],
    )


# 🔍 Trouver produit Airalo
def find_product(package_id):
    product = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "name", "list_price"], "limit": 1},
    )
    return product[0] if product else None


# 🟡 Sync commandes Airalo
def sync_airalo_orders():
    print("🛒 Sync commandes Airalo...")
    rows = supabase.table("airalo_orders").select("*").execute().data

    for row in rows:

        order_ref = row.get("order_id") or row.get("id")
        if not order_ref:
            print(f"⚠️ Commande ignorée (pas d'order_id) : {row}")
            continue

        email = row.get("email")
        package_id = row.get("package_id")

        if not email or not package_id:
            print(f"⚠️ Commande {order_ref} ignorée (email ou package_id manquant)")
            continue

        full_name = f"{row.get('prenom','')} {row.get('nom','')}".strip()
        created_at = row.get("created_at") or datetime.datetime.now().isoformat()

        # Déjà créée ?
        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "search",
            [[["client_order_ref", "=", order_ref]]],
            {"limit": 1},
        )
        if existing:
            continue

        partner_id = find_or_create_partner(email, full_name)
        product = find_product(package_id)
        if not product:
            print(f"❌ Produit introuvable pour commande Airalo : {package_id}")
            continue

        company_id, pricelist_id = get_company_and_pricelist(partner_id)

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "create",
            [{
                "partner_id": partner_id,
                "client_order_ref": order_ref,
                "company_id": company_id,
                "pricelist_id": pricelist_id,
                "date_order": created_at,
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


# 🛒 Sync commandes FenuaSim (standard)
def sync_orders():
    print("🛒 Sync commandes standard...")
    rows = supabase.table("orders").select("*").execute().data

    for row in rows:
        order_ref = row.get("order_id") or row.get("id")
        if not order_ref:
            print(f"⚠️ Commande ignorée (pas d'order_id) : {row}")
            continue

        email = row.get("email")
        package_id = row.get("package_id")

        if not email or not package_id:
            print(f"⚠️ Commande {order_ref} ignorée (email ou package_id manquant)")
            continue

        full_name = f"{row.get('prenom','')} {row.get('nom','')}".strip()
        created_at = row.get("created_at") or datetime.datetime.now().isoformat()

        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "search",
            [[["client_order_ref", "=", order_ref]]],
            {"limit": 1},
        )
        if existing:
            continue

        partner_id = find_or_create_partner(email, full_name)
        product = find_product(package_id)
        if not product:
            print(f"❌ Produit introuvable pour commande standard : {package_id}")
            continue

        company_id, pricelist_id = get_company_and_pricelist(partner_id)

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "create",
            [{
                "partner_id": partner_id,
                "client_order_ref": order_ref,
                "company_id": company_id,
                "pricelist_id": pricelist_id,
                "date_order": created_at,
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
