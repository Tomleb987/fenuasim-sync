import os
import requests
from supabase import create_client
import xmlrpc.client

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
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")


# -----------------------------
# FONCTION : récupérer le compte 706100
# -----------------------------
def get_esim_income_account():
    try:
        account = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "account.account", "search_read",
            [[["code", "=", "706100"]]],
            {"fields": ["id"], "limit": 1}
        )
        if account:
            print(f"✔ Compte 706100 trouvé : ID {account[0]['id']}")
            return account[0]["id"]
        else:
            print("⚠ Compte 706100 introuvable, aucun mapping produit possible.")
            return None
    except Exception as e:
        print("❌ Erreur lors de la récupération du compte 706100 :", e)
        return None


# -----------------------------
# FONCTION : synchronisation des produits
# -----------------------------
def sync_products():
    print("🚀 Synchronisation quotidienne des produits Airalo…")

    # Récupérer toutes les offres Airalo en base
    result = supabase.table("airalo_packages").select("*").execute()
    packages = result.data

    print(f"📦 {len(packages)} produits trouvés dans Supabase")

    esim_account_id = get_esim_income_account()

    for pkg in packages:

        package_id = pkg["id"]
        name = pkg["name"]
        region = pkg["region"]
        price = pkg.get("price", 0)

        # Recherche du produit existant dans Odoo
        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "search_read",
            [[["default_code", "=", package_id]]],
            {"fields": ["id", "name"], "limit": 1},
        )

        # Valeurs communes
        vals = {
            "name": f"{name} [{region}]" if region else name,
            "default_code": package_id,
            "list_price": float(price),
            "type": "service",
            "sale_ok": True,
            "purchase_ok": False,
            "property_account_income_id": esim_account_id,
        }

        if existing:
            product_id = existing[0]["id"]
            models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "product.product", "write",
                [[product_id], vals],
            )
            print(f"🔁 Produit mis à jour : {product_id} → {package_id}")
        else:
            product_id = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "product.product", "create",
                [vals],
            )
            print(f"✨ Nouveau produit créé : {product_id} → {package_id}")

    print("✅ Synchronisation des produits terminée.")


# -----------------------------
# MAIN
# -----------------------------
if __name__ == "__main__":
    sync_products()
