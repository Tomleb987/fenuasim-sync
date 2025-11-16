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
    print(f"🆕 Partenaire créé : {email} (ID {partner_id})")
    return partner_id


def get_income_account_706100():
    """Retourne l'ID du compte 706100 s'il existe."""
    account_ids = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "account.account",
        "search",
        [[["code", "=", "706100"]]],
        {"limit": 1},
    )
    if account_ids:
        return account_ids[0]
    print("⚠️ Compte 706100 introuvable dans Odoo, le produit sera créé sans compte dédié.")
    return None


def get_or_create_category_forfaits():
    """Retourne ou crée la catégorie produit 'Forfaits eSIM'."""
    categ = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "product.category",
        "search_read",
        [[["name", "=", "Forfaits eSIM"]]],
        {"fields": ["id"], "limit": 1},
    )
    if categ:
        return categ[0]["id"]

    categ_id = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "product.category",
        "create",
        [{"name": "Forfaits eSIM"}],
    )
    print(f"🆕 Catégorie produit créée : Forfaits eSIM (ID {categ_id})")
    return categ_id


def get_or_create_product(package_id, package_name=None, price=0.0,
                          data_amount=None, data_unit=None, validity_days=None):
    """
    Trouve ou crée un produit Odoo à partir du package_id (Airalo ID).
    - Affecte le compte 706100
    - Met la catégorie 'Forfaits eSIM'
    - Type service
    """
    if not package_id:
        raise ValueError("package_id manquant pour get_or_create_product")

    # 1) Cherche d'abord un produit existant
    product = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "product.product",
        "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "name", "list_price"], "limit": 1},
    )
    if product:
        return product[0]["id"], product[0]["name"]

    # 2) Sinon, on le crée
    account_id = get_income_account_706100()
    categ_id = get_or_create_category_forfaits()

    # Construction d'un nom lisible
    base_name = package_name or package_id
    details = []

    if data_amount and data_unit:
        details.append(f"{data_amount} {data_unit}")
    if validity_days:
        details.append(f"{validity_days} jours")

    if details:
        full_name = f"{base_name} - " + " / ".join(details)
    else:
        full_name = base_name

    vals = {
        "name": full_name,
        "default_code": package_id,
        "list_price": float(price or 0.0),
        "type": "service",
        "sale_ok": True,
        "purchase_ok": False,
    }

    if categ_id:
        vals["categ_id"] = categ_id
    if account_id:
        # Champ propriété revenu
        vals["property_account_income_id"] = account_id

    product_id = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "product.product",
        "create",
        [vals],
    )
    print(f"🆕 Produit créé automatiquement : {full_name} (ID {product_id}, code {package_id})")
    return product_id, full_name


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
    """Confirme une commande Odoo si possible (action_confirm)."""
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
        print(f"❌ Erreur passage en payé : {e}")


# -----------------------
# SYNC COMMANDES AIRALO (VENTES TECHNIQUES)
# -----------------------
def sync_airalo_orders():
    print("🔄 Sync Airalo orders…")
    rows = supabase.table("airalo_orders").select("*").execute().data
    print(f"📄 {len(rows)} lignes Airalo récupérées.")

    for row in rows:
        email = row.get("email")
        package_id = row.get("package_id")
        order_ref = row.get("order_id")  # ID Airalo (ex. 1134571)
        created_at = normalize_date(row.get("created_at"))

        if not email or not package_id or not order_ref:
            print("⚠️ Ligne Airalo incomplète, ignorée.")
            continue

        # Si déjà en Odoo → on ne recrée pas
        if find_odoo_order(order_ref):
            continue

        # On garantit qu'un produit existe pour ce package_id
        product_id, product_name = get_or_create_product(
            package_id=package_id,
            package_name=row.get("package_name") or package_id,
            price=0.0  # prix mis à jour par le job daily produits
        )

        partner_id = find_or_create_partner(email, email)

        # Création du devis Airalo dans Odoo
        order_id = models.execute_kw(
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
                                "product_id": product_id,
                                "name": product_name,
                                "product_uom_qty": 1,
                                "price_unit": 0.0,
                            },
                        )
                    ],
                }
            ],
        )
        print(f"🟢 Commande Airalo créée : {order_ref} (ID {order_id})")

    print("✅ Commandes Airalo synchronisées.")


# -----------------------
# SYNC PAIEMENTS STRIPE (TABLE orders)
# -----------------------
def sync_stripe_payments():
    print("💳 Sync Stripe…")
    rows = supabase.table("orders").select("*").execute().data
    print(f"📄 {len(rows)} lignes orders récupérées.")

    for row in rows:
        status = row.get("status", "")
        email = row.get("email")
        stripe_session_id = row.get("stripe_session_id")
        package_id = row.get("package_id")
        created_at = normalize_date(row.get("created_at"))

        if status != "completed":
            continue

        print(f"🔎 Stripe row → {email} | status={status}")

        if not stripe_session_id:
            print(f"⚠️ Stripe : pas de stripe_session_id pour {email}, ignoré.")
            continue

        # Si la commande existe déjà en Odoo, on ne recrée pas
        odoo_order_id = find_odoo_order(stripe_session_id)

        # On (re)garantit qu'il y a un produit
        package_name = row.get("package_name") or package_id
        data_amount = row.get("data_amount")
        data_unit = row.get("data_unit")
        validity = row.get("validity")
        amount = row.get("amount") or 0

        # montant Supabase → généralement en "unités" (ex: 300 = 3€) → à adapter si besoin
        price_eur = float(amount) / 100.0 if amount and amount > 0 else 0.0

        if not package_id:
            print(f"⚠️ Stripe : package_id manquant pour {stripe_session_id}, ignoré.")
            continue

        product_id, product_name = get_or_create_product(
            package_id=package_id,
            package_name=package_name,
            price=price_eur,
            data_amount=data_amount,
            data_unit=data_unit,
            validity_days=validity,
        )

        partner_display_name = row.get("nom") or row.get("last_name") or email
        partner_id = find_or_create_partner(email, partner_display_name)

        # Si commande n'existe pas encore → on crée
        if not odoo_order_id:
            line_desc_parts = []
            if package_name:
                line_desc_parts.append(str(package_name))
            if data_amount and data_unit:
                line_desc_parts.append(f"{data_amount} {data_unit}")
            if validity:
                line_desc_parts.append(f"{validity} jours")

            if line_desc_parts:
                line_name = " - ".join(line_desc_parts)
            else:
                line_name = product_name

            order_vals = {
                "partner_id": partner_id,
                "client_order_ref": stripe_session_id,
                "date_order": created_at,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": product_id,
                            "name": line_name,
                            "product_uom_qty": 1,
                            "price_unit": price_eur if price_eur > 0 else 0.0,
                        },
                    )
                ],
            }

            odoo_order_id = models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "sale.order",
                "create",
                [order_vals],
            )
            print(f"🧾 Commande Stripe créée (ID {odoo_order_id}) pour {stripe_session_id}")
        else:
            print(f"ℹ️ Commande Stripe déjà existante dans Odoo (ID {odoo_order_id})")

        # Tentative de confirmation (passe le devis en commande)
        confirm_order(odoo_order_id)

    print("✅ Sync Stripe terminé.")


# -----------------------
# MAIN
# -----------------------
if __name__ == "__main__":
    print("🚀 FAST SYNC STARTED")

    # Airalo : s'assure que les commandes techniques sont présentes
    sync_airalo_orders()

    # Stripe : crée les commandes clients payées + auto-création produits
    sync_stripe_payments()

    print("✅ FAST SYNC DONE")
