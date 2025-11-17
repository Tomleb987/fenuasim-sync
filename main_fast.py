import os
import sys
import time
import datetime
import base64
import xmlrpc.client

import requests
from supabase import create_client, Client

# ============================================================
#  CONFIG SUPABASE & ODOO
# ============================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ SUPABASE_URL ou SUPABASE_KEY manquants.")
    sys.exit(1)

if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
    print("❌ Paramètres Odoo manquants (ODOO_URL / ODOO_DB / ODOO_USER / ODOO_PASSWORD).")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Connexion Odoo
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    print("❌ Impossible de s'authentifier sur Odoo.")
    sys.exit(1)

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

ESIM_CATEGORY_ID = None           # cache catégorie produits
AIRALO_PACKAGES_CACHE = {}        # cache produits airalo_packages (Option 2)


# ============================================================
#  HELPERS ODOO — PARTENAIRES & CATÉGORIES
# ============================================================

def get_or_create_esim_category():
    """Retourne l'ID de la catégorie produit 'Forfaits eSIM', la crée si besoin."""
    global ESIM_CATEGORY_ID
    if ESIM_CATEGORY_ID:
        return ESIM_CATEGORY_ID

    cat_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.category", "search",
        [[("name", "=", "Forfaits eSIM")]],
        {"limit": 1},
    )
    if cat_ids:
        ESIM_CATEGORY_ID = cat_ids[0]
        return ESIM_CATEGORY_ID

    # Chercher le compte 706100 (recettes forfaits eSIM)
    income_account_id = False
    try:
        acc_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "account.account", "search",
            [[("code", "=", "706100")]],
            {"limit": 1},
        )
        if acc_ids:
            income_account_id = acc_ids[0]
    except Exception:
        income_account_id = False

    vals = {"name": "Forfaits eSIM"}
    if income_account_id:
        vals["property_account_income_categ_id"] = income_account_id

    cat_id = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.category", "create",
        [vals],
    )
    ESIM_CATEGORY_ID = cat_id
    print(f"🆕 Catégorie produit créée : Forfaits eSIM (ID {cat_id})")
    return ESIM_CATEGORY_ID


def ensure_partner(email, first_name=None, last_name=None):
    """Retourne l'ID du partenaire Odoo (res.partner), le crée si nécessaire."""
    if not email:
        email = "inconnu@fenuasim.com"

    partner_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "search",
        [[("email", "=", email)]],
        {"limit": 1},
    )
    if partner_ids:
        return partner_ids[0]

    if first_name or last_name:
        name = f"{first_name or ''} {last_name or ''}".strip()
    else:
        name = email

    partner_id = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "create",
        [{
            "name": name,
            "email": email,
        }],
    )
    print(f"🆕 Partner créé : {email} (ID {partner_id})")
    return partner_id


# ============================================================
#  HELPERS PRODUITS — OPTION 2 AVEC airalo_packages
# ============================================================

def load_airalo_packages_cache():
    """Charge tous les produits airalo_packages dans un cache mémoire."""
    global AIRALO_PACKAGES_CACHE
    print("🔁 Chargement du cache airalo_packages…")

    try:
        res = supabase.table("airalo_packages").select("*").execute()
        rows = res.data or []
    except Exception as e:
        print("⚠️ Impossible de charger la table airalo_packages :", e)
        AIRALO_PACKAGES_CACHE = {}
        return

    cache = {}
    for pkg in rows:
        # On indexe par plusieurs clés possibles (selon ton schéma Supabase)
        keys = []
        if pkg.get("id"):
            keys.append(str(pkg["id"]))
        if pkg.get("package_id"):
            keys.append(str(pkg["package_id"]))
        if pkg.get("airalo_id"):
            keys.append(str(pkg["airalo_id"]))
        if pkg.get("stripe_price_id"):
            keys.append(str(pkg["stripe_price_id"]))

        for k in keys:
            cache[k] = pkg

    AIRALO_PACKAGES_CACHE = cache
    print(f"📦 Cache airalo_packages chargé : {len(AIRALO_PACKAGES_CACHE)} entrées.")


def _search_product_by_code(default_code):
    """Recherche un produit Odoo par code interne (default_code)."""
    if not default_code:
        return None
    prod_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search",
        [[("default_code", "=", default_code)]],
        {"limit": 1},
    )
    return prod_ids[0] if prod_ids else None


def _build_label(data_amount, data_unit, validity, fallback_name):
    parts = []
    if fallback_name:
        parts.append(fallback_name)
    if data_amount and data_unit:
        parts.append(f"{data_amount} {data_unit}")
    if validity:
        parts.append(f"{validity} jours")
    return " - ".join(parts) if parts else "Forfait eSIM"


def get_or_create_product_from_package(pkg):
    """Crée / récupère un produit Odoo à partir d'un enregistrement airalo_packages."""
    categ_id = get_or_create_esim_category()

    default_code = (
        pkg.get("code")
        or pkg.get("package_id")
        or pkg.get("airalo_id")
        or f"PKG-{pkg.get('id')}"
    )
    default_code = str(default_code)

    existing = _search_product_by_code(default_code)
    if existing:
        return existing

    name = pkg.get("name") or "Forfait eSIM"
    data_amount = pkg.get("data_amount")
    data_unit = pkg.get("data_unit")
    validity = pkg.get("validity")

    label = _build_label(data_amount, data_unit, validity, name)

    # Prix : on privilégie le champ price du package, sinon 0
    price = pkg.get("price") or 0.0
    try:
        price = float(price)
    except Exception:
        price = 0.0

    vals = {
        "name": label,
        "default_code": default_code,
        "type": "service",
        "detailed_type": "service",
        "list_price": price,
        "categ_id": categ_id,
        "taxes_id": [(6, 0, [])],  # Pas de TVA
    }

    product_id = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "create",
        [vals],
    )
    print(f"🆕 Produit créé depuis airalo_packages : {label} (ID {product_id}, code {default_code})")
    return product_id


def get_or_create_product_from_order_row(row):
    """Fallback : crée / récupère un produit à partir de la ligne orders (sans airalo_packages)."""
    categ_id = get_or_create_esim_category()

    package_id = row.get("package_id")
    if not package_id:
        # backup basique
        package_id = f"ESIM-{row.get('id') or row.get('stripe_session_id')}"
    default_code = str(package_id)

    existing = _search_product_by_code(default_code)
    if existing:
        return existing

    package_name = row.get("package_name") or "Forfait eSIM"
    data_amount = row.get("data_amount")
    data_unit = row.get("data_unit")
    validity = row.get("validity")

    label = _build_label(data_amount, data_unit, validity, package_name)

    price = row.get("price")
    if price is None:
        amount = row.get("amount") or 0
        try:
            price = float(amount) / 100.0
        except Exception:
            price = 0.0
    else:
        try:
            price = float(price)
        except Exception:
            price = 0.0

    vals = {
        "name": label,
        "default_code": default_code,
        "type": "service",
        "detailed_type": "service",
        "list_price": price,
        "categ_id": categ_id,
        "taxes_id": [(6, 0, [])],
    }

    product_id = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "create",
        [vals],
    )
    print(f"🆕 Produit créé automatiquement : {label} (ID {product_id}, code {default_code})")
    return product_id


def get_product_for_order(row):
    """
    Retourne (product_id, package_record ou None) pour une ligne Stripe.
    - Si possible, on utilise airalo_packages (Option 2).
    - Sinon, fallback sur les données de la table orders.
    """
    pkg = None
    package_key = None

    # On essaie plusieurs clés pour retrouver le package dans le cache
    for key_field in ("package_id", "airalo_id", "stripe_price_id"):
        v = row.get(key_field)
        if v:
            package_key = str(v)
            if package_key in AIRALO_PACKAGES_CACHE:
                pkg = AIRALO_PACKAGES_CACHE[package_key]
                break

    if pkg:
        product_id = get_or_create_product_from_package(pkg)
        return product_id, pkg

    # Fallback
    product_id = get_or_create_product_from_order_row(row)
    return product_id, None


# ============================================================
#  HELPERS COMMANDES / FACTURES
# ============================================================

def find_odoo_order_by_stripe_ref(stripe_session_id):
    """Recherche une commande Odoo liée à une session Stripe (client_order_ref)."""
    if not stripe_session_id:
        return None

    order_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "search",
        [[("client_order_ref", "=", stripe_session_id)]],
        {"limit": 1},
    )
    return order_ids[0] if order_ids else None


def ensure_order_has_line(order_id, product_id, row):
    """
    S'assure que la sale.order a au moins une ligne avec un produit.
    Si aucune ligne OU pas de product_id sur les lignes => on en ajoute une.
    """
    price = row.get("price")
    if price is None:
        amount = row.get("amount") or 0
        try:
            price = float(amount) / 100.0
        except Exception:
            price = 0.0
    else:
        try:
            price = float(price)
        except Exception:
            price = 0.0

    package_name = row.get("package_name") or "Forfait eSIM"
    data_amount = row.get("data_amount")
    data_unit = row.get("data_unit")
    validity = row.get("validity")

    line_label_parts = [package_name]
    if data_amount and data_unit:
        line_label_parts.append(f"{data_amount} {data_unit}")
    if validity:
        line_label_parts.append(f"{validity} jours")
    line_name = " - ".join(line_label_parts)

    order_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "read",
        [[order_id], ["order_line"]],
    )[0]
    line_ids = order_data.get("order_line", [])

    if line_ids:
        lines = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order.line", "read",
            [line_ids, ["product_id"]],
        )
        has_product = any(l.get("product_id") and l["product_id"][0] for l in lines)
        if has_product:
            return  # déjà ok

    # Ajouter une ligne avec produit
    models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "write",
        [[order_id], {
            "order_line": [
                (0, 0, {
                    "product_id": product_id,
                    "name": line_name,
                    "product_uom_qty": 1.0,
                    "price_unit": price,
                })
            ]
        }],
    )


def confirm_order(order_id):
    """Tente de confirmer la commande (passage du devis à la commande)."""
    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "action_confirm",
            [[order_id]],
        )
        print(f"✅ Commande confirmée dans Odoo (ID {order_id})")
    except Exception as e:
        msg = str(e)
        if "ne sont pas dans un état nécessitant une confirmation" in msg:
            print(f"ℹ️ Commande déjà confirmée (ID {order_id})")
        else:
            print(f"❌ Erreur lors de la confirmation de la commande {order_id} :", e)


def find_invoice_for_order(order_id):
    """Recherche une facture client (out_invoice) liée à la commande."""
    order = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "read",
        [[order_id], ["name"]],
    )[0]
    order_name = order["name"]

    inv_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "account.move", "search",
        [[("move_type", "=", "out_invoice"), ("invoice_origin", "=", order_name)]],
        {"limit": 1},
    )
    return inv_ids[0] if inv_ids else None


def ensure_invoice_for_order(order_id):
    """
    Crée une facture pour la commande si elle n'existe pas encore.
    Utilise la méthode publique action_create_invoice.
    Retourne l'ID de la facture (account.move) ou None.
    """
    existing_invoice_id = find_invoice_for_order(order_id)
    if existing_invoice_id:
        return existing_invoice_id

    try:
        # Méthode publique qui encapsule _create_invoices
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "action_create_invoice",
            [[order_id]],
        )
    except Exception as e:
        print(f"❌ Erreur lors de la création de la facture pour la commande {order_id} :", e)
        return None

    invoice_id = find_invoice_for_order(order_id)
    if invoice_id:
        print(f"🧾 Facture créée pour la commande {order_id} (ID facture {invoice_id})")
    else:
        print(f"⚠️ Aucun account.move trouvé après création de facture pour la commande {order_id}")

    return invoice_id


# ============================================================
#  IMAGE SUR LA FACTURE (PIÈCE JOINTE)
# ============================================================

def attach_product_image_to_invoice(invoice_id, package_record):
    """
    Attache une image (visuel du forfait) en pièce jointe de la facture.
    L'image provient du record airalo_packages (champ image_url ou thumbnail_url).
    """
    if not package_record:
        return

    img_url = (
        package_record.get("image_url")
        or package_record.get("thumbnail_url")
        or package_record.get("icon_url")
    )
    if not img_url:
        return

    try:
        resp = requests.get(img_url, timeout=10)
        resp.raise_for_status()
        img_bytes = resp.content
        img_b64 = base64.b64encode(img_bytes).decode("utf-8")
    except Exception as e:
        print(f"⚠️ Impossible de récupérer l'image ({img_url}) pour la facture {invoice_id} :", e)
        return

    name = package_record.get("name") or "Forfait eSIM"
    filename = f"Visuel eSIM - {name}.png"

    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "ir.attachment", "create",
            [{
                "name": filename,
                "res_model": "account.move",
                "res_id": invoice_id,
                "type": "binary",
                "datas": img_b64,
                "mimetype": "image/png",
            }],
        )
        print(f"🖼 Image de forfait attachée à la facture {invoice_id} ({filename})")
    except Exception as e:
        print(f"⚠️ Erreur lors de la création de la pièce jointe pour la facture {invoice_id} :", e)


# ============================================================
#  SYNC AIRALO ORDERS (INFORMATIF)
# ============================================================

def sync_airalo_orders():
    """
    Synchronisation des commandes Airalo depuis Supabase vers Odoo.
    Objectif : suivi informatif (ICCID, QR code, etc.) sans impact comptable.
    """
    print("🔄 Sync Airalo orders…")

    res = supabase.table("airalo_orders").select("*").order("created_at", desc=False).execute()
    rows = res.data or []
    print(f"📄 {len(rows)} lignes Airalo récupérées.")

    created_count = 0

    for row in rows:
        order_id_airalo = row.get("order_id")
        email = row.get("email")
        prenom = row.get("prenom") or row.get("first_name")
        nom = row.get("nom") or row.get("last_name")
        iccid = row.get("sim_iccid")
        qr_code_url = row.get("qr_code_url")
        apple_url = row.get("apple_installation_url")
        status = row.get("status")
        data_balance = row.get("data_balance")
        created_at = row.get("created_at")

        if not order_id_airalo:
            continue

        client_order_ref = f"AIRALO-{order_id_airalo}"

        so_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "search",
            [[("client_order_ref", "=", client_order_ref)]],
            {"limit": 1},
        )
        if so_id:
            continue

        partner_id = ensure_partner(email, prenom, nom)

        note_parts = [
            f"Commande Airalo #{order_id_airalo}",
            f"Statut: {status}",
        ]
        if iccid:
            note_parts.append(f"ICCID: {iccid}")
        if qr_code_url:
            note_parts.append(f"QR: {qr_code_url}")
        if apple_url:
            note_parts.append(f"Apple install: {apple_url}")
        if data_balance:
            note_parts.append(f"Data restante: {data_balance}")
        if created_at:
            note_parts.append(f"Date création: {created_at}")

        note = "\n".join(note_parts)

        vals = {
            "partner_id": partner_id,
            "client_order_ref": client_order_ref,
            "origin": "Airalo",
            "note": note,
        }

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "create",
            [vals],
        )
        created_count += 1

    print("✅ Commandes Airalo synchronisées.")


# ============================================================
#  SYNC STRIPE PAYMENTS (COMMANDES + FACTURES + IMAGE)
# ============================================================

def sync_stripe_payments():
    """
    Synchronisation des paiements Stripe (table orders) vers Odoo.
    Pour chaque paiement 'completed' :
      - 1 commande client (sale.order) par session Stripe.
      - 1 produit eSIM (Option 2, basé sur airalo_packages si possible).
      - 1 ligne de commande avec ce produit.
      - Confirmation de la commande.
      - Création de la facture (account.move out_invoice) si inexistante.
      - Attachement du visuel produit à la facture (image).
    """
    print("💳 Sync Stripe…")

    res = supabase.table("orders").select("*").order("created_at", desc=False).execute()
    rows = res.data or []
    print(f"📄 {len(rows)} lignes orders récupérées.")

    for row in rows:
        status = row.get("status")
        email = row.get("email")
        stripe_session_id = row.get("stripe_session_id")

        if status != "completed":
            continue

        print(f"🔎 Stripe row → {email} | status={status}")

        if not stripe_session_id:
            print(f"⚠️ Ignoré : pas de stripe_session_id pour {email}")
            continue

        first_name = row.get("first_name") or row.get("prenom")
        last_name = row.get("last_name") or row.get("nom")

        partner_id = ensure_partner(email, first_name, last_name)
        product_id, pkg = get_product_for_order(row)

        existing_so_id = find_odoo_order_by_stripe_ref(stripe_session_id)

        if existing_so_id:
            order_id = existing_so_id
            print(f"ℹ️ Commande Stripe déjà existante dans Odoo (ID {order_id})")
        else:
            package_name = row.get("package_name") or "Forfait eSIM"
            data_amount = row.get("data_amount")
            data_unit = row.get("data_unit")
            validity = row.get("validity")

            label_parts = [package_name]
            if data_amount and data_unit:
                label_parts.append(f"{data_amount} {data_unit}")
            if validity:
                label_parts.append(f"{validity} jours")
            description = " - ".join(label_parts)

            vals = {
                "partner_id": partner_id,
                "client_order_ref": stripe_session_id,
                "origin": "Stripe",
                "note": description,
            }

            order_id = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "sale.order", "create",
                [vals],
            )
            print(f"🧾 Commande Stripe créée (ID {order_id})")

        # Ligne de commande
        ensure_order_has_line(order_id, product_id, row)

        # Confirmation commande
        confirm_order(order_id)

        # Facture
        invoice_id = ensure_invoice_for_order(order_id)
        if invoice_id:
            attach_product_image_to_invoice(invoice_id, pkg)

    print("✅ Sync Stripe terminé.")


# ============================================================
#  MAIN
# ============================================================

if __name__ == "__main__":
    print("🚀 FAST SYNC STARTED")
    start = time.time()

    # 1) Charger les produits airalo_packages (Option 2)
    load_airalo_packages_cache()

    # 2) Sync Airalo (informative)
    sync_airalo_orders()

    # 3) Sync Stripe (commandes + factures + image)
    sync_stripe_payments()

    duration = time.time() - start
    print("✅ FAST SYNC DONE")
    print(f"⏱ Durée : {duration:.1f}s")
