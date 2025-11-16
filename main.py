mport os
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
if not uid:
    raise RuntimeError("❌ Authentification Odoo échouée")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")


# -----------------------
# UTILS
# -----------------------
def normalize_date(date_value):
    """Normalise une date ISO en format Odoo '%Y-%m-%d %H:%M:%S'."""
    if not date_value:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        dt = datetime.fromisoformat(str(date_value).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def find_or_create_partner(email, name=None):
    """Trouve ou crée un partenaire Odoo à partir de l'email."""
    if not email:
        raise ValueError("Email manquant pour find_or_create_partner")

    partners = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "res.partner",
        "search_read",
        [[["email", "=", email]]],
        {"fields": ["id", "name"], "limit": 1},
    )
    if partners:
        return partners[0]["id"]

    partner_id = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "res.partner",
        "create",
        [
            {
                "name": name or email,
                "email": email,
                "customer_rank": 1,
            }
        ],
    )
    print(f"👤 Partenaire créé : {email} (ID {partner_id})")
    return partner_id


def find_product(package_id):
    """Retourne le produit Odoo (dict) à partir du default_code / package_id."""
    if not package_id:
        return None
    product = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "product.product",
        "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "name", "list_price"], "limit": 1},
    )
    return product[0] if product else None


def find_odoo_order(order_ref):
    """Retrouve l'ID d'une sale.order à partir du client_order_ref."""
    if not order_ref:
        return None
    res = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "sale.order",
        "search",
        [[["client_order_ref", "=", order_ref]]],
        {"limit": 1},
    )
    return res[0] if res else None


def confirm_order(order_id):
    """Confirme une commande Odoo si possible."""
    if not order_id:
        return
    try:
        models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "sale.order",
            "action_confirm",
            [[order_id]],
        )
        print(f"✅ Commande confirmée : {order_id}")
    except Exception as e:
        print(f"❌ Erreur confirmation commande {order_id} : {e}")


# -----------------------
# SYNC PRODUITS
# -----------------------
def sync_products():
    print("🚀 Sync produits Airalo...")
    packages = supabase.table("airalo_packages").select("*").execute().data
    print(f"📦 {len(packages)} packages trouvés")

    for row in packages:
        package_id = row.get("airalo_id")
        name = row.get("name")
        region = row.get("region") or ""
        price = row.get("final_price_eur") or row.get("price_eur") or 0.0

        if not package_id or not name:
            continue

        existing_ids = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "product.product",
            "search",
            [[["default_code", "=", package_id]]],
            {"limit": 1},
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
            models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "product.product",
                "write",
                [[existing_ids[0]], vals],
            )
            print(f"🔁 Produit mis à jour : {package_id}")
        else:
            models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "product.product",
                "create",
                [vals],
            )
            print(f"✅ Produit créé : {package_id}")

    print("🎉 Produits synchronisés.")


# -----------------------
# SYNC COMMANDES AIRALO
# -----------------------
def sync_airalo_orders():
    print("🛒 Sync commandes Airalo...")
    rows = supabase.table("airalo_orders").select("*").execute().data
    print(f"📄 {len(rows)} lignes airalo_orders")

    for row in rows:
        email = row.get("email")
        package_id = row.get("package_id")
        order_ref = row.get("order_id")
        created_at = normalize_date(row.get("created_at"))

        if not email or not package_id or not order_ref:
            print("⚠️ Ligne airalo_orders incomplète, ignorée.")
            continue

        # Déjà en Odoo ?
        if find_odoo_order(order_ref):
            continue

        product = find_product(package_id)
        if not product:
            print(f"❌ Produit introuvable Airalo : {package_id}")
            continue

        partner_id = find_or_create_partner(email, email)

        models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "sale.order",
            "create",
            [
                {
                    "partner_id": partner_id,
                    "client_order_ref": order_ref,
                    "date_order": created_at,
                    "order_line": [
                        (
                            0,
                            0,
                            {
                                "product_id": product["id"],
                                "name": product["name"],
                                "product_uom_qty": 1,
                                "price_unit": product["list_price"],
                            },
                        )
                    ],
                }
            ],
        )
        print(f"🟢 Commande Airalo créée : {order_ref}")

    print("✅ Commandes Airalo synchronisées.")


# -----------------------
# SYNC PAIEMENTS STRIPE (TABLE orders)
# -----------------------
def sync_stripe_payments():
    print("💳 Sync paiements Stripe (table orders)...")
    rows = supabase.table("orders").select("*").execute().data
    print(f"📄 {len(rows)} lignes orders")

    for row in rows:
        # order_id peut être absent → on sécurise
        order_ref = row.get("order_id")
        if not order_ref:
            print("⚠️ Paiement sans order_id, ignoré.")
            continue

        status = row.get("status", "")
        email = row.get("email")
        package_id = row.get("package_id")
        created_at = normalize_date(row.get("created_at"))

        if status != "completed":
            # On ne traite que les paiements réellement payés
            continue

        # Cherche une commande existante liée à cet order_id
        odoo_order_id = find_odoo_order(order_ref)

        # Si la commande n'existe pas encore en Odoo, on la crée
        if not odoo_order_id:
            if not email or not package_id:
                print(f"⚠️ Impossible de créer la commande pour {order_ref} (email ou package_id manquant)")
                continue

            product = find_product(package_id)
            if not product:
                print(f"❌ Produit introuvable (Stripe) : {package_id}")
                continue

            partner_id = find_or_create_partner(email, email)

            odoo_order_id = models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "sale.order",
                "create",
                [
                    {
                        "partner_id": partner_id,
                        "client_order_ref": order_ref,
                        "date_order": created_at,
                        "order_line": [
                            (
                                0,
                                0,
                                {
                                    "product_id": product["id"],
                                    "name": product["name"],
                                    "product_uom_qty": 1,
                                    "price_unit": product["list_price"],
                                },
                            )
                        ],
                    }
                ],
            )
            print(f"🟢 Commande créée via Stripe : {order_ref}")

        # On confirme la commande (qu'elle vienne d'Airalo ou de Stripe)
        confirm_order(odoo_order_id)

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
