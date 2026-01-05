import os
import xmlrpc.client
from supabase import create_client

# -----------------------------
# CONFIG
# -----------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Connexions Odoo
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

ESIM_CATEGORY_ID = None

# -----------------------------
# HELPERS
# -----------------------------

def get_or_create_esim_category():
    """Récupère ou crée la catégorie 'Forfaits eSIM'."""
    global ESIM_CATEGORY_ID
    if ESIM_CATEGORY_ID:
        return ESIM_CATEGORY_ID

    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.category", "search",
        [[("name", "=", "Forfaits eSIM")]],
        {"limit": 1}
    )
    if ids:
        ESIM_CATEGORY_ID = ids[0]
    else:
        ESIM_CATEGORY_ID = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.category", "create",
            [{"name": "Forfaits eSIM"}]
        )
        print("🆕 Catégorie 'Forfaits eSIM' créée.")
    return ESIM_CATEGORY_ID

def get_esim_income_account():
    """Récupère le compte comptable 706100."""
    try:
        account = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "account.account", "search_read",
            [[["code", "=", "706100"]]],
            {"fields": ["id"], "limit": 1}
        )
        if account:
            return account[0]["id"]
        print("⚠ Compte 706100 introuvable.")
        return None
    except Exception as e:
        print("❌ Erreur récupération compte 706100 :", e)
        return None

# -----------------------------
# SYNCHRONISATION DES PRODUITS
# -----------------------------
def sync_products():
    print("🚀 Synchronisation des produits Airalo (Optimisée)...")

    # Récupérer les offres Airalo depuis Supabase
    result = supabase.table("airalo_packages").select("*").execute()
    packages = result.data
    print(f"📦 {len(packages)} produits trouvés dans Supabase.")

    esim_account_id = get_esim_income_account()
    categ_id = get_or_create_esim_category()

    for pkg in packages:
        package_id = pkg["id"]
        raw_name = pkg["name"]
        region = pkg["region"]
        price = pkg.get("price", 0)

        # NETTOYAGE : On retire les mentions de validité (ex: "30 jours", "7 days")
        # On garde le nom de base et la région
        clean_name = raw_name
        if region:
            clean_name = f"{clean_name} [{region}]"

        # Recherche du produit existant
        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "search",
            [[["default_code", "=", package_id]]],
            {"limit": 1},
        )

        vals = {
            "name": clean_name,
            "default_code": package_id,
            "list_price": float(price),
            "type": "service",
            "sale_ok": True,
            "purchase_ok": False,
            "categ_id": categ_id,
            "property_account_income_id": esim_account_id,
        }

        if existing:
            models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "product.product", "write",
                [[existing[0]], vals],
            )
            print(f"🔁 Mis à jour : {package_id}")
        else:
            product_id = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "product.product", "create",
                [vals],
            )
            print(f"✨ Créé : {clean_name} ({package_id})")

    print("✅ Synchronisation des produits terminée.")

# -----------------------------
# MAIN
# -----------------------------
if __name__ == "__main__":
    sync_products()
