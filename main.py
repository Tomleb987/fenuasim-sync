import os
import base64
import requests
import xmlrpc.client
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# ------------------------------------------------------------------
# 🔐 CONFIGURATION
# ------------------------------------------------------------------
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
if not uid:
    raise Exception("❌ Connexion Odoo échouée")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# ------------------------------------------------------------------
# 🔧 HELPERS
# ------------------------------------------------------------------
def normalize_dt(value):
    """Convertit ISO 8601 → YYYY-MM-DD HH:MM:SS pour Odoo SaaS"""
    if not value:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_image_base64(url):
    if not url:
        return None
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return base64.b64encode(r.content).decode("utf-8")
    except:
        pass
    return None


# ------------------------------------------------------------------
# 🧹 SUPPRESSION DES DOUBLONS PRODUITS
# ------------------------------------------------------------------
def remove_duplicate_products():
    print("🧹 Suppression des doublons…")

    products = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "!=", False]]],
        {"fields": ["id", "default_code"], "limit": 5000},
    )

    grouped = {}
    for p in products:
        grouped.setdefault(p["default_code"], []).append(p["id"])

    total = 0
    for code, ids in grouped.items():
        if len(ids) > 1:
            try:
                models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "product.product", "unlink", [ids[1:]],
                )
                total += len(ids) - 1
                print(f"🗑️ Doublons supprimés pour {code}")
            except:
                pass

    print(f"✅ Nettoyage terminé ({total} supprimés)")

# ------------------------------------------------------------------
# 🔄 CREATION / MISE À JOUR PRODUITS AIRALO
# ------------------------------------------------------------------
def upsert_product(row):
    package_id = row.get("airalo_id")
    if not package_id:
        return

    name = row.get("name")
    region = row.get("region") or ""
    description = row.get("description") or ""
    price = row.get("final_price_eur") or row.get("price_eur") or 0.0
    image_url = row.get("image_url")

    full_name = f"{name} [{region}]" if region else name

    desc = []
    if description:
        desc.append(description)
    if row.get("data_amount") and row.get("data_unit") and row.get("validity_days"):
        desc.append(f"{row['data_amount']} {row['data_unit']} pour {row['validity_days']} jours")
    if region:
        desc.append(f"Région : {region}")

    full_desc = "\n".join(desc)

    existing = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "product_tmpl_id", "image_1920"], "limit": 1},
    )

    img = None

    # Mise à jour
    if existing:
        tmpl_id = existing[0]["product_tmpl_id"][0]
        if not existing[0]["image_1920"] and image_url:
            img = get_image_base64(image_url)

        vals = {"name": full_name, "list_price": float(price), "description": full_desc}
        if img:
            vals["image_1920"] = img

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.template", "write",
            [[tmpl_id], vals],
        )

        print(f"🔁 Produit mis à jour : {package_id}")
        return

    # Création
    if image_url:
        img = get_image_base64(image_url)

    vals = {
        "name": full_name,
        "default_code": package_id,
        "list_price": float(price),
        "type": "service",
        "sale_ok": True,
        "purchase_ok": False,
        "description": full_desc,
    }
    if img:
        vals["image_1920"] = img

    models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "create", [vals]
    )

    print(f"✅ Produit créé : {package_id}")

# ------------------------------------------------------------------
# 🔁 SYNC PRODUITS AIRALO
# ------------------------------------------------------------------
def sync_airalo_packages():
    print("🚀 Sync produits Airalo…")
    rows = supabase.table("airalo_packages").select("*").execute().data
    for row in rows:
        upsert_product(row)
    print("🎉 Produits synchronisés.\n")

# ------------------------------------------------------------------
# UTILISATEURS / PRODUITS
# ------------------------------------------------------------------
def find_or_create_partner(email, name):
    res = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "search", [[["email", "=", email]]], {"limit": 1}
    )
    if res:
        return res[0]
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "create",
        [{"name": name or email, "email": email, "customer_rank": 1}],
    )

def find_product(package_id):
    res = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "name", "list_price"], "limit": 1}
    )
    return res[0] if res else None


# ------------------------------------------------------------------
# SYNC COMMANDES AIRALO
# ------------------------------------------------------------------
def sync_airalo_orders():
    print("🛒 Sync commandes Airalo…")
    rows = supabase.table("airalo_orders").select("*").execute().data

    for row in rows:
        order_ref = row.get("order_id")
        email = row.get("email")
        package_id = row.get("package_id")

        # Correction anciens codes
        if "discover+" in package_id:
            pkg = package_id.replace("discover+", "discover")
            package_id = pkg

        if not order_ref or not email or not package_id:
            continue

        # Déjà existante ?
        so = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "search",
            [[["client_order_ref", "=", order_ref]]],
            {"limit": 1}
        )
        if so:
            continue

        partner = find_or_create_partner(email, email)
        product = find_product(package_id)
        if not product:
            print(f"❌ Produit introuvable Airalo : {package_id}")
            continue

        date_order = normalize_dt(row.get("created_at"))

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "create",
            [{
                "partner_id": partner,
                "client_order_ref": order_ref,
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

# ------------------------------------------------------------------
# SYNC COMMANDES standard
# ------------------------------------------------------------------
def sync_orders():
    print("🛒 Sync commandes standard…")
    rows = supabase.table("orders").select("*").execute().data

    for row in rows:
        order_ref = row.get("order_id")
        email = row.get("email")
        package_id = row.get("package_id")

        if not order_ref or not email or not package_id:
            continue

        # TOPUP converti en produit unique
        if package_id.isdigit():
            package_id = "topup-" + package_id

        # Correction discover+
        if "discover+" in package_id:
            package_id = package_id.replace("discover+", "discover")

        # Déjà existante ?
        so = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "search",
            [[["client_order_ref", "=", order_ref]]],
            {"limit": 1}
        )
        if so:
            continue

        partner = find_or_create_partner(email, email)
        product = find_product(package_id)
        if not product:
            print(f"❌ Produit introuvable commande standard : {package_id}")
            continue

        date_order = normalize_dt(row.get("created_at"))

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "create",
            [{
                "partner_id": partner,
                "client_order_ref": order_ref,
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

# ------------------------------------------------------------------
# 🔥 CONFIRMATION DES COMMANDES PAYÉES (STRIPE)
# ------------------------------------------------------------------
def sync_stripe_payments():
    print("💳 Confirmation des commandes Stripe…")

    rows = supabase.table("orders").select("*").execute().data

    for row in rows:
        order_ref = row.get("order_id")
        payment_status = (row.get("payment_status") or "").lower()

        if payment_status not in ("succeeded", "paid", "completed"):
            continue

        # Trouver la commande Odoo
        so_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "search",
            [[["client_order_ref", "=", order_ref]]],
            {"limit": 1}
        )

        if not so_ids:
            print(f"⚠️ Paiement OK mais commande introuvable : {order_ref}")
            continue

        so_id = so_ids[0]

        # Lire état
        state = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "read",
            [[so_id]], {"fields": ["state"]}
        )[0]["state"]

        if state == "sale":
            continue  # déjà confirmé

        # Confirmation
        try:
            models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "sale.order", "action_confirm",
                [[so_id]]
            )
            print(f"✅ Commande confirmée (Stripe) : {order_ref}")
        except Exception as e:
            print(f"❌ Erreur confirmation {order_ref} : {e}")

# ------------------------------------------------------------------
# 🚀 MAIN
# ------------------------------------------------------------------
if __name__ == "__main__":
    print("🚀 Début synchronisation Supabase → Odoo\n")

    remove_duplicate_products()
    sync_airalo_packages()
    sync_airalo_orders()
    sync_orders()
    sync_stripe_payments()

    print("✅ Synchronisation terminée")
