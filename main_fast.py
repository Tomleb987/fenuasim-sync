import os
import sys
import time
import datetime
import xmlrpc.client

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

ESIM_CATEGORY_ID = None  # cache global pour la catégorie produit eSIM


# ============================================================
#  HELPERS ODOO
# ============================================================

def get_or_create_esim_category():
    """Retourne l'ID de la catégorie produit 'Forfaits eSIM', la crée si besoin."""
    global ESIM_CATEGORY_ID
    if ESIM_CATEGORY_ID:
        return ESIM_CATEGORY_ID

    # Chercher la catégorie existante
    cat_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'product.category', 'search',
        [[('name', '=', 'Forfaits eSIM')]],
        {'limit': 1}
    )
    if cat_ids:
        ESIM_CATEGORY_ID = cat_ids[0]
        return ESIM_CATEGORY_ID

    # Chercher le compte 706100 (facultatif si non trouvé)
    income_account_id = False
    try:
        acc_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'account.account', 'search',
            [[('code', '=', '706100')]],
            {'limit': 1}
        )
        if acc_ids:
            income_account_id = acc_ids[0]
    except Exception:
        income_account_id = False

    vals = {
        'name': 'Forfaits eSIM',
    }
    if income_account_id:
        vals['property_account_income_categ_id'] = income_account_id

    cat_id = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'product.category', 'create',
        [vals]
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
        'res.partner', 'search',
        [[('email', '=', email)]],
        {'limit': 1}
    )
    if partner_ids:
        return partner_ids[0]

    if first_name or last_name:
        name = f"{first_name or ''} {last_name or ''}".strip()
    else:
        name = email

    partner_id = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'res.partner', 'create',
        [{
            'name': name,
            'email': email,
        }]
    )
    print(f"🆕 Partner créé : {email} (ID {partner_id})")
    return partner_id


def get_or_create_product_for_order(row):
    """
    Crée / récupère un produit Odoo à partir d'une ligne Stripe (table orders).
    Clé = package_id (Option A).
    """
    package_id = row.get('package_id')
    package_name = row.get('package_name') or "Forfait eSIM"
    data_amount = row.get('data_amount')
    data_unit = row.get('data_unit')
    validity = row.get('validity')

    if not package_id:
        # fallback très rare
        package_id = f"ESIM-{data_amount or ''}{data_unit or ''}-{validity or ''}j"

    # Chercher produit par code interne = package_id
    product_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'product.product', 'search',
        [[('default_code', '=', package_id)]],
        {'limit': 1}
    )
    if product_ids:
        return product_ids[0]

    # Création du produit
    categ_id = get_or_create_esim_category()

    # Prix : priorité à field "price", sinon amount/100
    price = row.get('price')
    if price is None:
        amount = row.get('amount') or 0
        price = float(amount) / 100.0
    else:
        price = float(price)

    # Nom lisible
    label_parts = []
    if data_amount and data_unit:
        label_parts.append(f"{data_amount} {data_unit}")
    if validity:
        label_parts.append(f"{validity} jours")
    label = " - ".join(label_parts) if label_parts else package_name

    vals = {
        'name': f"{label}",
        'default_code': package_id,
        'type': 'service',
        'detailed_type': 'service',
        'list_price': price,
        'categ_id': categ_id,
        'taxes_id': [(6, 0, [])],  # Pas de TVA
    }

    product_id = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'product.product', 'create',
        [vals]
    )
    print(f"🆕 Produit créé automatiquement : {label} (ID {product_id}, code {package_id})")
    return product_id


def find_odoo_order_by_stripe_ref(stripe_session_id):
    """Recherche une commande Odoo liée à une session Stripe (client_order_ref)."""
    if not stripe_session_id:
        return None

    order_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'sale.order', 'search',
        [[('client_order_ref', '=', stripe_session_id)]],
        {'limit': 1}
    )
    if order_ids:
        return order_ids[0]
    return None


def ensure_order_has_line(order_id, product_id, row):
    """
    S'assure que la sale.order a au moins une ligne avec un produit.
    Si aucune ligne / aucune ligne avec product_id => on ajoute une ligne.
    """
    # Prix
    price = row.get('price')
    if price is None:
        amount = row.get('amount') or 0
        price = float(amount) / 100.0
    else:
        price = float(price)

    # Libellé de ligne
    package_name = row.get('package_name') or "Forfait eSIM"
    data_amount = row.get('data_amount')
    data_unit = row.get('data_unit')
    validity = row.get('validity')

    line_label_parts = [package_name]
    if data_amount and data_unit:
        line_label_parts.append(f"{data_amount} {data_unit}")
    if validity:
        line_label_parts.append(f"{validity} jours")
    line_name = " - ".join(line_label_parts)

    # Lire les lignes existantes
    order_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'sale.order', 'read',
        [[order_id], ['order_line']]
    )[0]
    line_ids = order_data.get('order_line', [])

    if line_ids:
        # Vérifier si au moins une ligne a déjà un produit
        lines = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order.line', 'read',
            [line_ids, ['product_id']]
        )
        has_product = any(l.get('product_id') and l['product_id'][0] for l in lines)
        if has_product:
            # Rien à faire
            return

    # Ajouter une nouvelle ligne avec produit
    models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'sale.order', 'write',
        [[order_id], {
            'order_line': [
                (0, 0, {
                    'product_id': product_id,
                    'name': line_name,
                    'product_uom_qty': 1.0,
                    'price_unit': price,
                })
            ]
        }]
    )


def confirm_order(order_id):
    """Tente de confirmer la commande (passage du devis à la commande)."""
    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'action_confirm',
            [[order_id]]
        )
        print(f"✅ Commande confirmée dans Odoo (ID {order_id})")
    except Exception as e:
        print(f"❌ Erreur passage en payé :", e)


# ============================================================
#  SYNC AIRALO ORDERS (technique / info)
# ============================================================

def sync_airalo_orders():
    """
    Synchronisation des commandes Airalo depuis Supabase vers Odoo.
    Ici : on crée un devis informatif par commande Airalo (sans produit),
    surtout pour le suivi (ICCID, QR code, etc.).
    """
    print("🔄 Sync Airalo orders…")

    res = supabase.table("airalo_orders").select("*").order("created_at", desc=False).execute()
    rows = res.data or []
    print(f"📄 {len(rows)} lignes Airalo récupérées.")

    created_count = 0

    for row in rows:
        order_id_airalo = row.get('order_id')
        email = row.get('email')
        prenom = row.get('prenom') or row.get('first_name')
        nom = row.get('nom') or row.get('last_name')
        iccid = row.get('sim_iccid')
        qr_code_url = row.get('qr_code_url')
        apple_url = row.get('apple_installation_url')
        status = row.get('status')
        data_balance = row.get('data_balance')
        created_at = row.get('created_at')

        if not order_id_airalo:
            continue

        client_order_ref = f"AIRALO-{order_id_airalo}"

        # Chercher si déjà existante
        so_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'search',
            [[('client_order_ref', '=', client_order_ref)]],
            {'limit': 1}
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
            'partner_id': partner_id,
            'client_order_ref': client_order_ref,
            'origin': 'Airalo',
            'note': note,
        }

        new_so_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'create',
            [vals]
        )
        created_count += 1

    print("✅ Commandes Airalo synchronisées.")


# ============================================================
#  SYNC STRIPE PAYMENTS (commande + produit + confirmation)
# ============================================================

def sync_stripe_payments():
    """
    Synchronisation des paiements Stripe (table orders) vers Odoo.
    - 1 commande Odoo par session Stripe (client_order_ref = stripe_session_id)
    - 1 produit par package_id (Option A)
    - 1 ligne de commande avec ce produit
    - Confirmation de la commande
    """
    print("💳 Sync Stripe…")

    res = supabase.table("orders").select("*").order("created_at", desc=False).execute()
    rows = res.data or []
    print(f"📄 {len(rows)} lignes orders récupérées.")

    for row in rows:
        status = row.get('status')
        email = row.get('email')
        stripe_session_id = row.get('stripe_session_id')

        if status != 'completed':
            continue

        print(f"🔎 Stripe row → {email} | status={status}")

        if not stripe_session_id:
            print(f"⚠️ Ignoré : pas de stripe_session_id pour {email}")
            continue

        # Nom / prénom
        first_name = row.get('first_name') or row.get('prenom')
        last_name = row.get('last_name') or row.get('nom')

        partner_id = ensure_partner(email, first_name, last_name)
        product_id = get_or_create_product_for_order(row)

        # Chercher ou créer la commande Odoo
        existing_so_id = find_odoo_order_by_stripe_ref(stripe_session_id)

        if existing_so_id:
            print(f"ℹ️ Commande Stripe déjà existante dans Odoo (ID {existing_so_id})")
            order_id = existing_so_id
        else:
            # Créer une nouvelle commande
            package_name = row.get('package_name') or "Forfait eSIM"
            data_amount = row.get('data_amount')
            data_unit = row.get('data_unit')
            validity = row.get('validity')

            label_parts = [package_name]
            if data_amount and data_unit:
                label_parts.append(f"{data_amount} {data_unit}")
            if validity:
                label_parts.append(f"{validity} jours")
            description = " - ".join(label_parts)

            vals = {
                'partner_id': partner_id,
                'client_order_ref': stripe_session_id,
                'origin': 'Stripe',
                'note': description,
            }

            order_id = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'sale.order', 'create',
                [vals]
            )
            print(f"🧾 Commande Stripe créée (ID {order_id})")

        # S'assurer que la commande a au moins une ligne produit
        ensure_order_has_line(order_id, product_id, row)

        # Confirmer la commande
        confirm_order(order_id)

    print("✅ Sync Stripe terminé.")


# ============================================================
#  MAIN
# ============================================================

if __name__ == "__main__":
    print("🚀 FAST SYNC STARTED")
    start = time.time()

    sync_airalo_orders()
    sync_stripe_payments()

    duration = time.time() - start
    print("✅ FAST SYNC DONE")
    print(f"⏱ Durée : {duration:.1f}s")
