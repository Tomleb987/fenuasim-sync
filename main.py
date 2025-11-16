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
def normalize_dt(value: str) -> str:
    """Convertit ISO 8601 → YYYY-MM-DD HH:MM:SS pour Odoo SaaS."""
    if not value:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_image_base64(url: str):
    if not url:
        return None
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return base64.b64encode(r.content).decode("utf-8")
    except Exception:
        pass
    return None


# ------------------------------------------------------------------
# 🧹 SUPPRESSION DES DOUBLONS PRODUITS
# ------------------------------------------------------------------
def remove_duplicate_products():
    print("🧹 Suppression des doublons produits…")

    products = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "product.product",
        "search_read",
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
                    ODOO_DB,
                    uid,
                    ODOO_PASSWORD,
                    "product.product",
                    "unlink",
                    [ids[1:]],
                )
                total += len(ids) - 1
                print(f"🗑️ Doublons supprimés pour {code}")
            except Exception as e:
                print(f"❌ Erreur suppression doublons {code}: {e}")

    print(f"✅ Nettoyage terminé ({total} produits supprimés)\n")


# ------------------------------------------------------------------
# 🔄 CREATION / MISE À JOUR PRODUITS AIRALO
# ------------------------------------------------------------------
def upsert_product(row: dict):
    package_id = row.get("airalo_id")
    name = row.get("name")
    if not package_id or not name:
        return

    region = row.get("region") or ""
    description = row.get("description") or ""
    price = row.get("final_price_eur") or row.get("price_eur") or 0.0
    image_url = row.get("image_url")

    full_name = f"{name} [{region}]" if region else name

    desc_lines = []
    if description:
        desc_lines.append(description)
    if row.get("data_amount") and row.get("data_unit") and row.get("validity_days"):
        desc_lines.append(f"{row['data_amount']} {row['data_unit']} pour {row['validity_days']} jours")
    if region:
        desc_lines.append(f"Région : {region}")
    full_desc = "\n".join(desc_lines)

    existing = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "product.product",
        "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "product_tmpl_id", "image_1920"], "limit": 1},
    )

    image_b64 = None

    # Mise à jour
    if existing:
        prod = existing[0]
        tmpl_id = prod["product_tmpl_id"][0]

        if not prod["image_1920"] and image_url:
            image_b64 = get_image_base64(image_url)

        vals = {
            "name": full_name,
            "list_price": float(price),
            "description": full_desc,
        }
        if image_b64:
            vals["image_1920"] = image_b64

        models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "product.template",
            "write",
            [[tmpl_id], vals],
        )
        print(f"🔁 Produit mis à jour : {package_id}")
        return

    # Création
    if image_url:
        image_b64 = get_image_base64(image_url)

    vals = {
        "name": full_name,
        "default_code": package_id,
        "list_price": float(price),
        "type": "service",
        "sale_ok": True,
        "purchase_ok": False,
        "description": full_desc,
    }
    if image_b64:
        vals["image_1920"] = image_b64

    models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "product.product",
        "create",
        [vals],
    )
    print(f"✅ Produit créé : {package_id}")


def sync_airalo_packages():
    print("🚀 Sync produits Airalo…")
    rows = supabase.table("airalo_packages").select("*").execute().data
    print(f"📦 {len(rows)} packages trouvés")
    for row in rows:
        upsert_product(row)
    print("🎉 Produits Airalo synchronisés.\n")


# ------------------------------------------------------------------
# PARTENAIRES & PRODUITS
# ------------------------------------------------------------------
def find_or_create_partner(email: str, full_name: str):
    partners = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "res.partner",
        "search",
        [[["email", "=", email]]],
        {"limit": 1},
    )
    if partners:
        return partners[0]

    return models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "res.partner",
        "create",
        [{"name": full_name or email, "email": email, "customer_rank": 1}],
    )


def find_product(package_id: str):
    products = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "product.product",
        "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "name", "list_price"], "limit": 1},
    )
    return products[0] if products else None


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

        if not order_ref or not email or not package_id:
            continue

        # Correction anciens codes "discover+"
        if "discover+" in package_id:
            package_id = package_id.replace("discover+", "discover")

        existing = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "sale.order",
            "search",
            [[["client_order_ref", "=", order_ref]]],
            {"limit": 1},
        )
        if existing:
            continue

        full_name = f"{row.get('prenom', '')} {row.get('nom', '')}".strip() or email
        partner_id = find_or_create_partner(email, full_name)
        product = find_product(package_id)
        if not product:
            print(f"❌ Produit introuvable Airalo : {package_id}")
            continue

        date_order = normalize_dt(row.get("created_at"))

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
                    "date_order": date_order,
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

    print("✅ Commandes Airalo synchronisées.\n")


# ------------------------------------------------------------------
# SYNC COMMANDES STANDARD (SITE FENUA SIM)
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

        # TOPUP : les ids numériques deviennent des codes produit "topup-XXXX"
        if package_id.isdigit():
            package_id = f"topup-{package_id}"

        # Correction anciens codes "discover+"
        if "discover+" in package_id:
            package_id = package_id.replace("discover+", "discover")

        existing = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "sale.order",
            "search",
            [[["client_order_ref", "=", order_ref]]],
            {"limit": 1},
        )
        if existing:
            continue

        full_name = f"{row.get('prenom', '')} {row.get('nom', '')}".strip() or email
        partner_id = find_or_create_partner(email, full_name)
        product = find_product(package_id)
        if not product:
            print(f"❌ Produit introuvable commande standard : {package_id}")
            continue

        date_order = normalize_dt(row.get("created_at"))

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
                    "date_order": date_order,
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

        print(f"🟢 Commande standard créée : {order_ref}")

    print("✅ Commandes standard synchronisées.\n")


# ------------------------------------------------------------------
# FACTURATION : CREATION + VALIDATION
# ------------------------------------------------------------------
def create_and_validate_invoice(order_id):
    """Crée puis valide la facture d'une commande confirmée."""

    order = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "sale.order",
        "read",
        [[order_id]],
        {"fields": ["invoice_ids", "state"]},
    )[0]

    # Déjà facturée ?
    if order["invoice_ids"]:
        return order["invoice_ids"][0]

    if order["state"] != "sale":
        print(f"⚠️ Impossible de facturer : commande non confirmée (id {order_id})")
        return None

    # Création de la facture
    invoice_ids = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "sale.order",
        "action_create_invoice",
        [[order_id]],
    )

    if not invoice_ids:
        print(f"❌ Aucune facture créée pour la commande {order_id}")
        return None

    invoice_id = invoice_ids[0]
    print(f"🧾 Facture créée : {invoice_id}")

    # Validation de la facture
    models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "account.move",
        "action_post",
        [[invoice_id]],
    )

    print(f"📬 Facture validée : {invoice_id}")
    return invoice_id


# ------------------------------------------------------------------
# PAIEMENT STRIPE : CREATION + RAPPROCHEMENT
# ------------------------------------------------------------------
def register_stripe_payment(invoice_id, amount, stripe_id):
    """Enregistre le paiement Stripe dans Odoo et rapproche la facture."""
    try:
        invoice = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "account.move",
            "read",
            [[invoice_id]],
            {"fields": ["partner_id", "currency_id", "company_id"]},
        )[0]
    except Exception as e:
        print(f"❌ Impossible de lire la facture {invoice_id} : {e}")
        return None

    partner_id = invoice["partner_id"][0]
    currency_id = invoice["currency_id"][0]
    company_id = invoice["company_id"][0]

    # Journal de type "bank" par défaut
    journals = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "account.journal",
        "search_read",
        [[["type", "=", "bank"]]],
        {"fields": ["id", "name"], "limit": 1},
    )
    if not journals:
        print("❌ Aucun journal de type 'bank' trouvé pour enregistrer le paiement.")
        return None

    journal_id = journals[0]["id"]

    payment_vals = {
        "amount": float(amount),
        "payment_type": "inbound",
        "partner_type": "customer",
        "partner_id": partner_id,
        "journal_id": journal_id,
        "currency_id": currency_id,
        "ref": f"Stripe {stripe_id}",
        "company_id": company_id,
        # méthode de paiement par défaut (souvent 'Manual')
        "payment_method_line_id": 1,
    }

    payment_id = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "account.payment",
        "create",
        [payment_vals],
    )

    print(f"💰 Paiement créé : {payment_id}")

    models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "account.payment",
        "action_post",
        [[payment_id]],
    )

    print(f"🏁 Paiement validé et rapproché avec la facture {invoice_id}")
    return payment_id


# ------------------------------------------------------------------
# SYNC PAIEMENTS STRIPE -> CONFIRMATION + FACTURE + PAIEMENT
# ------------------------------------------------------------------
def sync_stripe_payments():
    print("💳 Sync paiements Stripe (table orders)…")

    rows = supabase.table("orders").select("*").execute().data

    for row in rows:
        order_ref = row.get("order_id")
        status = (row.get("status") or "").lower()
        amount = row.get("price") or 0.0
        stripe_id = row.get("stripe_session_id") or order_ref

        # On ne traite que les paiements réussis
        if status not in ("succeeded", "paid", "completed"):
            continue

        if not order_ref:
            print("⚠️ Paiement sans order_id, ignoré.")
            continue

        # Chercher la commande
        so_ids = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "sale.order",
            "search",
            [[["client_order_ref", "=", order_ref]]],
            {"limit": 1},
        )
        if not so_ids:
            print(f"⚠️ Paiement Stripe OK mais commande introuvable : {order_ref}")
            continue

        so_id = so_ids[0]

        # Lire l'état de la commande
        order_data = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "sale.order",
            "read",
            [[so_id]],
            {"fields": ["state"]},
        )[0]

        # Confirmer la commande si nécessaire
        if order_data["state"] != "sale":
            try:
                models.execute_kw(
                    ODOO_DB,
                    uid,
                    ODOO_PASSWORD,
                    "sale.order",
                    "action_confirm",
                    [[so_id]],
                )
                print(f"✅ Commande confirmée (Stripe) : {order_ref}")
            except Exception as e:
                print(f"❌ Erreur confirmation commande {order_ref} : {e}")
                continue

        # Créer + valider la facture
        invoice_id = create_and_validate_invoice(so_id)
        if not invoice_id:
            continue

        # Si la facture est déjà payée, on ne recrée pas de paiement
        inv_data = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "account.move",
            "read",
            [[invoice_id]],
            {"fields": ["payment_state"]},
        )[0]
        if inv_data.get("payment_state") == "paid":
            continue

        # Enregistrer le paiement Stripe
        register_stripe_payment(invoice_id, amount, stripe_id)

    print("✅ Paiements Stripe synchronisés.\n")


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
