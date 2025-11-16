import os
import base64
import requests
import xmlrpc.client
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime

# Stripe
import stripe

load_dotenv()

# =====================================================================
# 🔐 VARIABLES ENVIRONNEMENT
# =====================================================================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
STRIPE_SECRET = os.getenv("STRIPE_SECRET_KEY")

if STRIPE_SECRET:
    stripe.api_key = STRIPE_SECRET

# Vérification clés
if not all([SUPABASE_URL, SUPABASE_KEY, ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
    raise RuntimeError("❌ Variables d'environnement manquantes.")

# =====================================================================
# 🔗 CONNEXIONS
# =====================================================================
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise Exception("❌ Connexion Odoo échouée")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")


# =====================================================================
# ⏱️ NORMALISATION DATETIME (Odoo SaaS)
# =====================================================================
def normalize_odoo_datetime(value):
    if not value:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# =====================================================================
# 🏢 COMPANY + PRICELIST
# =====================================================================
def get_company_and_pricelist(partner_id):
    user_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "res.users", "read",
        [uid], {"fields": ["company_id"]}
    )[0]
    company_id = user_data["company_id"][0]

    partner_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "read",
        [partner_id], {"fields": ["property_product_pricelist"]}
    )[0]
    pricelist_id = partner_data["property_product_pricelist"][0]

    return company_id, pricelist_id


# =====================================================================
# 📸 IMAGE → BASE64
# =====================================================================
def get_image_base64_from_url(url):
    if not url:
        return None

    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            return base64.b64encode(resp.content).decode("utf-8")
        else:
            print(f"⚠️ Erreur image {url} → {resp.status_code}")
    except Exception as e:
        print(f"❌ Exception image : {e}")

    return None


# =====================================================================
# 🧹 SUPPRESSION DES DOUBLONS PRODUITS
# =====================================================================
def remove_duplicate_products():
    print("🧹 Suppression des doublons produits…")

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

    deleted = 0
    for code, ids in grouped.items():
        if len(ids) > 1:
            try:
                models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "product.product", "unlink",
                    [ids[1:]],
                )
                deleted += len(ids) - 1
                print(f"🗑️ Doublons supprimés : {code}")
            except Exception as e:
                print(f"❌ Erreur suppression {code} : {e}")

    print(f"✅ Nettoyage terminé ({deleted} supprimés)")


# =====================================================================
# 🔄 CREATION / MISE À JOUR PRODUITS
# =====================================================================
def upsert_product(row):
    package_id = row.get("airalo_id")
    name_base = row.get("name")
    region = row.get("region") or ""
    price = float(row.get("final_price_eur") or row.get("price_eur") or 0.0)
    description = row.get("description") or ""
    data_amount = row.get("data_amount")
    data_unit = row.get("data_unit")
    validity_days = row.get("validity_days")
    image_url = row.get("image_url")

    if not package_id or not name_base:
        return

    # Nom
    name = f"{name_base} [{region}]" if region else name_base

    # Description interne
    desc = []
    if description:
        desc.append(description)
    if data_amount and data_unit:
        desc.append(f"{data_amount} {data_unit} — {validity_days} jours")
    if region:
        desc.append(f"Région : {region}")

    full_desc = "\n".join(desc)

    # Recherche produit existant
    existing = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "product_tmpl_id", "image_1920"], "limit": 1},
    )

    # MISE À JOUR
    if existing:
        tmpl_id = existing[0]["product_tmpl_id"][0]
        vals = {
            "name": name,
            "list_price": price,
            "description": full_desc,
        }

        if not existing[0]["image_1920"] and image_url:
            img = get_image_base64_from_url(image_url)
            if img:
                vals["image_1920"] = img

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.template", "write",
            [[tmpl_id], vals],
        )

        print(f"🔁 Produit mis à jour : {package_id}")
        return

    # CREATION
    img = None
    if image_url:
        img = get_image_base64_from_url(image_url)

    vals = {
        "name": name,
        "default_code": package_id,
        "list_price": price,
        "type": "service",
        "sale_ok": True,
        "purchase_ok": False,
        "description": full_desc,
    }
    if img:
        vals["image_1920"] = img

    models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "create",
        [vals],
    )

    print(f"✅ Produit créé : {package_id}")


def sync_airalo_packages():
    print("🚀 Sync produits Airalo…")
    rows = supabase.table("airalo_packages").select("*").execute().data

    for row in rows:
        upsert_product(row)

    print("🎉 Produits synchronisés.")


# =====================================================================
# 👥 PARTENAIRES
# =====================================================================
def find_or_create_partner(email, full_name):
    partner = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "search",
        [[["email", "=", email]]],
        {"limit": 1},
    )
    if partner:
        return partner[0]

    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "create",
        [{"name": full_name or email, "email": email, "customer_rank": 1}],
    )


# =====================================================================
# 🔎 PRODUIT PAR DEFAULT_CODE
# =====================================================================
def find_product(package_id):
    product = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "name", "list_price"], "limit": 1},
    )
    return product[0] if product else None


# =====================================================================
# 🛒 SYNC COMMANDES AIRALO
# =====================================================================
def sync_airalo_orders():
    print("🛒 Sync commandes Airalo…")

    rows = supabase.table("airalo_orders").select("*").execute().data

    for row in rows:
        order_ref = row.get("order_id") or row.get("id")
        if not order_ref:
            continue

        email = row.get("email")
        package_id = row.get("package_id")
        if not email or not package_id:
            continue

        full_name = f"{row.get('prenom','')} {row.get('nom','')}".strip()
        date_order = normalize_odoo_datetime(row.get("created_at"))

        # Déjà présent ?
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
            print(f"❌ Produit introuvable Airalo : {package_id}")
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
                "date_order": date_order,
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


# =====================================================================
# 🛒 COMMANDES FENUASIM
# =====================================================================
def sync_orders():
    print("🛒 Sync commandes standard…")

    rows = supabase.table("orders").select("*").execute().data

    for row in rows:
        order_ref = row.get("order_id") or row.get("id")
        if not order_ref:
            continue

        email = row.get("email")
        package_id = row.get("package_id")
        if not email or not package_id:
            continue

        full_name = f"{row.get('prenom','')} {row.get('nom','')}".strip()
        date_order = normalize_odoo_datetime(row.get("created_at"))

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
            print(f"❌ Produit introuvable commande standard : {package_id}")
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
                "date_order": date_order,
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


# =====================================================================
# 🧪 TEST STRIPE (optionnel)
# =====================================================================
def test_stripe():
    if not STRIPE_SECRET:
        print("⚠️ Stripe non configuré")
        return

    try:
        charges = stripe.Charge.list(limit=1)
        if charges.data:
            print(f"🟢 Stripe OK — dernière transaction : {charges.data[0]['id']}")
        else:
            print("🟡 Stripe OK — aucune transaction trouvée")
    except Exception as e:
        print(f"❌ Stripe error : {e}")


# =====================================================================
# 🚀 MAIN
# =====================================================================
if __name__ == "__main__":
    print("🚀 Début synchronisation Supabase → Odoo")

    test_stripe()
    remove_duplicate_products()
    sync_airalo_packages()
    sync_airalo_orders()
    sync_orders()

    print("✅ Synchronisation terminée")
